# url-spider
Crawls a website and writes out every page URL it finds. Forked from [Missing-alt-scraper](https://github.com/IslayAnderson/Missing-alt-scraper).

Pure Python 3 standard library, no installs, no browser driver.

## Usage

```
python3 spider.py https://example.com
```

Or put start URLs in `urls` (one per line) and run `python3 spider.py`.

Results go to `found_urls.txt`, progress goes to stderr.

| Option | Default | |
|---|---|---|
| `-o`, `--output` | `found_urls.txt` | output file |
| `-m`, `--max-pages` | `500` | stop after this many pages |
| `-d`, `--delay` | `0.5` | seconds between requests |
| `-t`, `--timeout` | `15` | request timeout |
| `--status` | off | write `status<TAB>url` instead of just the url |
| `--all-hosts` | off | follow links off the start domain(s) |
| `--ignore-robots` | off | skip the robots.txt check |

Only follows `<a>`/`<area>` links on HTML pages, skips `rel="nofollow"`, strips `#fragments`, and ignores obvious asset files (images, PDFs, CSS/JS, etc).

use and abuse my code ¯\_(ツ)_/¯
