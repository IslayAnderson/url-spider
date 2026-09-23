#!/usr/bin/env python3
"""Crawl a website and list every URL found on it.

Standard library only, no installs needed.
"""
import argparse
import sys
import time
from collections import deque
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

USER_AGENT = "url-spider/1.0 (+https://github.com/IslayAnderson/url-spider)"
SKIP_EXTENSIONS = (
	".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".avif",
	".pdf", ".zip", ".gz", ".mp3", ".mp4", ".mov", ".webm", ".woff", ".woff2",
	".css", ".js", ".xml", ".json", ".txt",
)


class LinkParser(HTMLParser):
	def __init__(self):
		super().__init__()
		self.links = []
		self.base = None

	def handle_starttag(self, tag, attrs):
		attrs = dict(attrs)
		if tag == "base" and attrs.get("href"):
			self.base = attrs["href"]
		elif tag in ("a", "area") and attrs.get("href"):
			if "nofollow" not in (attrs.get("rel") or "").lower():
				self.links.append(attrs["href"])


def normalise(url):
	url, _ = urldefrag(url)
	parts = urlparse(url)
	path = parts.path or "/"
	return parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower(), path=path).geturl()


def fetch(url, timeout):
	req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.1"})
	with urlopen(req, timeout=timeout) as resp:
		final_url = resp.geturl()
		ctype = resp.headers.get("Content-Type", "")
		if "html" not in ctype:
			return final_url, resp.status, None
		charset = resp.headers.get_content_charset() or "utf-8"
		return final_url, resp.status, resp.read().decode(charset, errors="replace")


def load_robots(start_url):
	parts = urlparse(start_url)
	rp = RobotFileParser(f"{parts.scheme}://{parts.netloc}/robots.txt")
	try:
		rp.read()
	except Exception:
		rp.allow_all = True
	return rp


def crawl(start_urls, max_pages, delay, timeout, same_host, respect_robots):
	queue = deque(normalise(u) for u in start_urls)
	seen = set(queue)
	hosts = {urlparse(u).netloc for u in queue}
	robots = {}
	results = []

	while queue and len(results) < max_pages:
		url = queue.popleft()
		host = urlparse(url).netloc

		if respect_robots:
			if host not in robots:
				robots[host] = load_robots(url)
			if not robots[host].can_fetch(USER_AGENT, url):
				print(f"robots  {url}", file=sys.stderr)
				continue

		try:
			final_url, status, html = fetch(url, timeout)
		except HTTPError as e:
			results.append((url, e.code))
			print(f"{e.code}     {url}", file=sys.stderr)
			continue
		except (URLError, TimeoutError, OSError) as e:
			results.append((url, "error"))
			print(f"error   {url} ({e})", file=sys.stderr)
			continue

		results.append((url, status))
		print(f"{status}     {url}", file=sys.stderr)

		if html:
			parser = LinkParser()
			try:
				parser.feed(html)
			except Exception:
				pass
			base = urljoin(final_url, parser.base) if parser.base else final_url
			for href in parser.links:
				link = normalise(urljoin(base, href.strip()))
				parts = urlparse(link)
				if parts.scheme not in ("http", "https"):
					continue
				if same_host and parts.netloc not in hosts:
					continue
				if parts.path.lower().endswith(SKIP_EXTENSIONS):
					continue
				if link not in seen:
					seen.add(link)
					queue.append(link)

		if delay:
			time.sleep(delay)

	return results


def main():
	ap = argparse.ArgumentParser(description="Crawl a site and list every URL found.")
	ap.add_argument("urls", nargs="*", help="start URL(s); defaults to the lines in ./urls")
	ap.add_argument("-o", "--output", default="found_urls.txt", help="output file (default: found_urls.txt)")
	ap.add_argument("-m", "--max-pages", type=int, default=500, help="stop after this many pages (default: 500)")
	ap.add_argument("-d", "--delay", type=float, default=0.5, help="seconds between requests (default: 0.5)")
	ap.add_argument("-t", "--timeout", type=float, default=15, help="request timeout in seconds (default: 15)")
	ap.add_argument("--status", action="store_true", help="include the HTTP status next to each URL")
	ap.add_argument("--all-hosts", action="store_true", help="follow links off the start domain(s)")
	ap.add_argument("--ignore-robots", action="store_true", help="don't check robots.txt")
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

	results = crawl(start, args.max_pages, args.delay, args.timeout,
		not args.all_hosts, not args.ignore_robots)

	with open(args.output, "w") as f:
		for url, status in results:
			f.write(f"{status}\t{url}\n" if args.status else f"{url}\n")

	print(f"\n{len(results)} URLs written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
	main()
