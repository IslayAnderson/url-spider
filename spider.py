#!/usr/bin/env python3
"""Crawl a website in a real (headless) browser and list every URL found on it.

Pages are rendered with Selenium, so links added by React, Vue, etc. are picked up.
"""
import argparse
import getpass
import os
import sys
import time
from collections import deque
from urllib.parse import urldefrag, urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException

SKIP_EXTENSIONS = (
	".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".avif",
	".pdf", ".zip", ".gz", ".mp3", ".mp4", ".mov", ".webm", ".woff", ".woff2",
	".css", ".js", ".json", ".txt",
)

# Sitemaps list URLs in <loc>, RSS in <link>text</link>, Atom and hreflang
# alternates in <link href="">.
XML_URLS_JS = """
function xmlUrls(doc, base) {
	const urls = [];
	for (const el of doc.getElementsByTagName('*')) {
		if (el.localName === 'loc') urls.push(el.textContent.trim());
		else if (el.localName === 'link') urls.push(el.getAttribute('href') || el.textContent.trim());
	}
	return urls.filter(Boolean).map(u => {
		try { return new URL(u, base).href; } catch (e) { return null; }
	}).filter(Boolean);
}
"""

# HTML: a.href / area.href are already resolved to absolute URLs by the browser.
LINKS_JS = XML_URLS_JS + """
const type = document.contentType || '';
if (type.includes('xml') && !type.includes('html')) return xmlUrls(document, document.baseURI);
return Array.from(document.querySelectorAll('a[href], area[href]'))
	.filter(a => !(a.rel || '').toLowerCase().includes('nofollow'))
	.map(a => a.href);
"""

# Feeds served as application/rss+xml etc. get downloaded rather than displayed,
# so fetch XML from inside the page instead (keeps cookies and basic auth).
FETCH_XML_JS = XML_URLS_JS + """
const [url, timeoutMs, done] = arguments;
const ctrl = new AbortController();
setTimeout(() => ctrl.abort(), timeoutMs);
fetch(url, {credentials: 'include', signal: ctrl.signal}).then(async r => {
	const type = r.headers.get('content-type') || '';
	if (!type.includes('xml') || type.includes('html')) return done(null);
	const doc = new DOMParser().parseFromString(await r.text(), 'application/xml');
	done({status: r.status, url: r.url, links: xmlUrls(doc, r.url)});
}).catch(() => done(null));
"""

STATUS_JS = """
const nav = performance.getEntriesByType('navigation')[0];
return nav && nav.responseStatus ? nav.responseStatus : null;
"""


def make_driver(browser, headless, bidi, page_timeout):
	if browser == "chrome":
		options = webdriver.ChromeOptions()
		if headless:
			options.add_argument("--headless=new")
	else:
		options = webdriver.FirefoxOptions()
		if headless:
			options.add_argument("-headless")
	options.enable_bidi = bidi
	# don't wait for every image/ad/tracker to finish; wait_for_links handles rendering
	options.page_load_strategy = "eager"
	driver = webdriver.Chrome(options=options) if browser == "chrome" else webdriver.Firefox(options=options)
	driver.set_page_load_timeout(page_timeout)
	driver.set_script_timeout(page_timeout + 5)
	return driver


def add_basic_auth(driver, username, password, hosts):
	"""Answer HTTP auth challenges from the crawled site(s); refuse everyone else."""
	attempts = {}

	def handler(request):
		if site(urlparse(request.url).netloc) not in hosts:
			request.cancel()
			return
		# a rejected login re-challenges straight away; don't loop on a wrong password
		attempts[request.url] = attempts.get(request.url, 0) + 1
		if attempts[request.url] > 2:
			request.cancel()
			return
		request.provide_credentials(username, password)

	driver.network.add_authentication_handler(handler)


def normalise(url):
	# keep hash-router fragments (#/about, #!/about), drop plain anchors
	base, frag = urldefrag(url)
	parts = urlparse(base)
	url = parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower(), path=parts.path or "/").geturl()
	if frag.startswith(("/", "!")):
		url += "#" + frag
	return url


def site(netloc):
	# treat www.example.com and example.com as the same site
	return netloc[4:] if netloc.startswith("www.") else netloc


def looks_like_xml(url):
	path = urlparse(url).path.lower()
	last = path.rstrip("/").rsplit("/", 1)[-1]
	return path.endswith(".xml") or last in ("feed", "rss", "atom")


def fetch_xml(driver, url, page_timeout):
	"""Fetch an XML document from inside the browser; None if it isn't XML or can't be fetched."""
	return driver.execute_async_script(FETCH_XML_JS, url, int(page_timeout * 1000))


def load(driver, url, page_timeout, max_wait, settle):
	"""Open a URL; returns (status, final url, links), or None if it never responded."""
	if looks_like_xml(url):
		data = fetch_xml(driver, url, page_timeout)
		if data:
			return data["status"], data["url"], data["links"]
	# tag the current page so we can tell if the next one never replaced it
	driver.execute_script("window.__spiderOld = true;")
	try:
		driver.get(url)
	except TimeoutException:
		# page is still loading something; stop it and use what's there
		driver.execute_script("window.stop();")
		if driver.execute_script("return window.__spiderOld === true;"):
			# never displayed: may be a download-type XML file, else it's dead
			data = fetch_xml(driver, url, page_timeout)
			return (data["status"], data["url"], data["links"]) if data else None
	links = wait_for_links(driver, max_wait, settle)
	status = driver.execute_script(STATUS_JS) or "-"
	# read the address once the page has settled, to catch server and client-side redirects
	return status, driver.current_url, links


def wait_for_links(driver, max_wait, settle):
	"""Wait for the page to load, then until its links stop changing for `settle` seconds.
	A page with no links yet keeps waiting (up to max_wait) in case the app hasn't rendered."""
	end = time.time() + max_wait
	while time.time() < end and driver.execute_script("return document.readyState") == "loading":
		time.sleep(0.2)
	links, stable_since = None, time.time()
	while links is None or time.time() < end:
		current = driver.execute_script(LINKS_JS)
		if current != links:
			links, stable_since = current, time.time()
		elif links and time.time() - stable_since >= settle:
			break
		time.sleep(0.25)
	return links or []


def crawl(new_driver, start_urls, hosts, max_pages, delay, page_timeout, max_wait, settle, same_host, results):
	queue = deque(normalise(u) for u in start_urls)
	seen = set(queue)
	start = set(queue)
	crawled = set()
	driver = new_driver()

	try:
		while queue and len(results) < max_pages:
			url = queue.popleft()
			try:
				loaded = load(driver, url, page_timeout, max_wait, settle)
				if loaded is None:
					results.append((url, "timeout"))
					print(f"timeout {url} (no response)", file=sys.stderr)
					continue
				status, final, links = loaded
				final = normalise(final)
				if not final.startswith(("http://", "https://")):
					final = url
				if url in start:
					# follow the site wherever the start URL redirected to
					hosts.add(site(urlparse(final).netloc))
				elif same_host and site(urlparse(final).netloc) not in hosts:
					print(f"skip   {url} (redirects off-site)", file=sys.stderr)
					continue
				if final != url:
					# redirected: record the page under where it landed, once
					if final in crawled:
						continue
					seen.add(final)
					url = final
				crawled.add(url)
			except WebDriverException as e:
				results.append((url, "error"))
				reason = "needs a login, use --auth" if "promptUserAndPass" in (e.msg or "") else e.msg
				print(f"error  {url} ({reason})", file=sys.stderr)
				continue
			except Exception as e:
				# the browser itself stopped responding: replace it and carry on
				results.append((url, "error"))
				print(f"error  {url} (browser hung: {type(e).__name__}), restarting browser", file=sys.stderr)
				try:
					driver.quit()
				except Exception:
					pass
				driver = new_driver()
				continue

			results.append((url, status))
			print(f"{status}    {url}  ({len(links)} links)", file=sys.stderr)

			for href in links:
				link = normalise(href)
				parts = urlparse(link)
				if parts.scheme not in ("http", "https"):
					continue
				if same_host and site(parts.netloc) not in hosts:
					continue
				if parts.path.lower().endswith(SKIP_EXTENSIONS):
					continue
				if link not in seen:
					seen.add(link)
					queue.append(link)

			if delay:
				time.sleep(delay)
	finally:
		driver.quit()


def main():
	ap = argparse.ArgumentParser(description="Crawl a site in a headless browser and list every URL found.")
	ap.add_argument("urls", nargs="*", help="start URL(s); defaults to the lines in ./urls")
	ap.add_argument("-o", "--output", default="found_urls.txt", help="output file (default: found_urls.txt)")
	ap.add_argument("-m", "--max-pages", type=int, default=500, help="stop after this many pages (default: 500)")
	ap.add_argument("-d", "--delay", type=float, default=0, help="extra seconds between pages (default: 0)")
	ap.add_argument("-w", "--wait", type=float, default=10, help="max seconds to wait for a page to render (default: 10)")
	ap.add_argument("-p", "--page-timeout", type=float, default=30, help="max seconds for a page to load before it's stopped (default: 30)")
	ap.add_argument("-s", "--settle", type=float, default=1.5, help="seconds the links must stay unchanged before moving on (default: 1.5)")
	ap.add_argument("-b", "--browser", choices=("firefox", "chrome"), default="firefox", help="browser to drive (default: firefox)")
	ap.add_argument("--show", action="store_true", help="show the browser window instead of running headless")
	ap.add_argument("--status", action="store_true", help="include the HTTP status next to each URL")
	ap.add_argument("-a", "--auth", metavar="USER[:PASS]", default=os.environ.get("SPIDER_AUTH"),
		help="HTTP basic auth for the start site(s); prompts for the password if omitted. Also read from $SPIDER_AUTH")
	ap.add_argument("--all-hosts", action="store_true", help="follow links off the start domain(s)")
	args = ap.parse_args()

	start = args.urls
	if not start:
		try:
			with open("urls") as f:
				start = [l.strip() for l in f if l.strip().startswith(("http://", "https://"))]
		except FileNotFoundError:
			pass
	if not start:
		ap.error("give a start URL, or put one per line in ./urls")

	hosts = {site(urlparse(normalise(u)).netloc) for u in start}
	password = None
	if args.auth:
		username, sep, password = args.auth.partition(":")
		if not sep:
			password = getpass.getpass(f"Password for {username}: ")

	def new_driver():
		driver = make_driver(args.browser, not args.show, bool(args.auth), args.page_timeout)
		if args.auth:
			add_basic_auth(driver, username, password, hosts)
		return driver

	results = []
	try:
		crawl(new_driver, start, hosts, args.max_pages, args.delay, args.page_timeout, args.wait, args.settle, not args.all_hosts, results)
	except KeyboardInterrupt:
		print("\nstopped, saving what was found so far", file=sys.stderr)

	with open(args.output, "w") as f:
		for url, status in results:
			f.write(f"{status}\t{url}\n" if args.status else f"{url}\n")

	print(f"\n{len(results)} URLs written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
	main()
