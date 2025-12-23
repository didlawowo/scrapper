"""
MCP Server for Scrapper - Web scraping tools for AI assistants.

This server exposes web scraping capabilities via the Model Context Protocol (MCP),
allowing AI assistants to fetch and parse web content when sites block AI crawlers.

Usage:
    python -m mcp_server
    # or
    python mcp_server.py
"""

import asyncio
import datetime
import os
from pathlib import Path

import tldextract
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from playwright.async_api import async_playwright, Browser, BrowserType

import settings
from internal import util, cache
from internal.browser import new_context, page_processing, get_screenshot
from internal.errors import ArticleParsingError, LinksParsingError
from router.query_params import (
    CommonQueryParams,
    BrowserQueryParams,
    ProxyQueryParams,
    ReadabilityQueryParams,
    LinkParserQueryParams,
)


# Global browser instance for MCP server
_browser: Browser | None = None
_semaphore: asyncio.Semaphore | None = None


async def get_browser() -> tuple[Browser, asyncio.Semaphore]:
    """Get or create browser instance."""
    global _browser, _semaphore

    if _browser is None or not _browser.is_connected():
        playwright = await async_playwright().start()
        browser_type: BrowserType = getattr(playwright, settings.BROWSER_TYPE.value)
        _browser = await browser_type.launch(headless=True)
        _semaphore = asyncio.Semaphore(settings.BROWSER_CONTEXT_LIMIT)

    return _browser, _semaphore


def create_params_from_args(args: dict) -> tuple[CommonQueryParams, BrowserQueryParams, ProxyQueryParams]:
    """Create parameter objects from tool arguments."""
    common = CommonQueryParams()
    common.cache = args.get('cache', False)
    common.full_content = args.get('full_content', False)
    common.screenshot = args.get('screenshot', False)
    common.user_scripts = None
    common.user_scripts_timeout = args.get('user_scripts_timeout', 0)

    browser = BrowserQueryParams()
    browser.incognito = args.get('incognito', True)
    browser.timeout = args.get('timeout', 60000)
    browser.wait_until = args.get('wait_until', 'domcontentloaded')
    browser.sleep = args.get('sleep', 0)
    browser.resource = None
    browser.viewport_width = args.get('viewport_width')
    browser.viewport_height = args.get('viewport_height')
    browser.screen_width = args.get('screen_width')
    browser.screen_height = args.get('screen_height')
    browser.device = args.get('device', 'Desktop Chrome')
    browser.scroll_down = args.get('scroll_down', 0)
    browser.ignore_https_errors = args.get('ignore_https_errors', True)
    browser.user_agent = args.get('user_agent')
    browser.locale = args.get('locale')
    browser.timezone = args.get('timezone')
    browser.http_credentials = None
    browser.extra_http_headers = None

    proxy = ProxyQueryParams()
    proxy.proxy_server = args.get('proxy_server')
    proxy.proxy_bypass = args.get('proxy_bypass')
    proxy.proxy_username = args.get('proxy_username')
    proxy.proxy_password = args.get('proxy_password')

    return common, browser, proxy


async def scrape_article_impl(url: str, args: dict) -> dict:
    """Implementation of article scraping."""
    browser, semaphore = await get_browser()
    common, browser_params, proxy_params = create_params_from_args(args)

    # Readability params
    readability = ReadabilityQueryParams()
    readability.max_elems_to_parse = args.get('max_elems_to_parse', 0)
    readability.nb_top_candidates = args.get('nb_top_candidates', 5)
    readability.char_threshold = args.get('char_threshold', 500)

    async with semaphore:
        async with new_context(browser, browser_params, proxy_params) as context:
            page = await context.new_page()
            await page_processing(
                page=page,
                url=url,
                params=common,
                browser_params=browser_params,
                init_scripts=[settings.READABILITY_SCRIPT],
            )
            page_content = await page.content()
            page_url = page.url

            # Parse article using Readability.js
            parser_args = {
                'maxElemsToParse': readability.max_elems_to_parse,
                'nbTopCandidates': readability.nb_top_candidates,
                'charThreshold': readability.char_threshold,
            }
            with open(settings.PARSER_SCRIPTS_DIR / 'article.js', encoding='utf-8') as f:
                article = await page.evaluate(f.read() % parser_args)

    if article is None:
        raise ArticleParsingError(page_url, "The page doesn't contain any articles.")

    if 'err' in article:
        raise ArticleParsingError(page_url, article['err'])

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    domain = tldextract.extract(page_url).registered_domain

    article['url'] = page_url
    article['domain'] = domain
    article['date'] = now
    article['meta'] = util.social_meta_tags(page_content)

    if common.full_content:
        article['fullContent'] = page_content

    if 'title' in article and 'content' in article:
        article['content'] = util.improve_content(
            title=article['title'],
            content=article['content'],
        )

    if 'textContent' in article:
        article['textContent'] = util.improve_text_content(article['textContent'])
        article['length'] = len(article['textContent']) - article['textContent'].count('\n')

    return article


async def scrape_links_impl(url: str, args: dict) -> dict:
    """Implementation of links scraping."""
    from collections import defaultdict
    from operator import itemgetter
    from statistics import median
    import hashlib

    browser, semaphore = await get_browser()
    common, browser_params, proxy_params = create_params_from_args(args)

    # Link parser params
    text_len_threshold = args.get('text_len_threshold', 40)
    words_threshold = args.get('words_threshold', 3)

    async with semaphore:
        async with new_context(browser, browser_params, proxy_params) as context:
            page = await context.new_page()
            await page_processing(
                page=page,
                url=url,
                params=common,
                browser_params=browser_params,
            )
            page_content = await page.content()
            page_url = page.url
            title = await page.title()

            # Parse links
            parser_args = {}
            with open(settings.PARSER_SCRIPTS_DIR / 'links.js', encoding='utf-8') as f:
                links = await page.evaluate(f.read() % parser_args)

    if 'err' in links:
        raise LinksParsingError(page_url, links['err'])

    # Filter and group links
    domain = tldextract.extract(url).domain

    def allowed_domain(href: str, domain: str) -> bool:
        if href.startswith('http'):
            return tldextract.extract(href).domain == domain
        return True

    def make_key(link: dict) -> str:
        props = (
            link['cssSel'],
            link['color'],
            link['font'],
            link['parentPadding'],
            link['parentMargin'],
            link['parentBgColor'],
        )
        s = '|'.join(props)
        return hashlib.sha1(s.encode()).hexdigest()[:7]

    links = [x for x in links if allowed_domain(x['href'], domain)]

    # Group links
    links_dict = defaultdict(list)
    for link in links:
        links_dict[make_key(link)].append(link)

    # Filter by statistics
    filtered_links = []
    for _, group in links_dict.items():
        median_text_len = median([len(x['text']) for x in group])
        median_words_count = median([len(x['words']) for x in group])
        if median_text_len > text_len_threshold and median_words_count > words_threshold:
            filtered_links.extend(group)

    # Sort and clean
    filtered_links.sort(key=itemgetter('pos'))
    cleaned_links = [{'url': link['url'], 'text': link['text']} for link in filtered_links]
    cleaned_links = list(map(util.improve_link, cleaned_links))

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    domain = tldextract.extract(page_url).registered_domain

    return {
        'url': page_url,
        'domain': domain,
        'date': now,
        'title': title,
        'links': cleaned_links,
        'meta': util.social_meta_tags(page_content),
    }


async def fetch_page_impl(url: str, args: dict) -> dict:
    """Implementation of page fetching."""
    browser, semaphore = await get_browser()
    common, browser_params, proxy_params = create_params_from_args(args)

    async with semaphore:
        async with new_context(browser, browser_params, proxy_params) as context:
            page = await context.new_page()
            await page_processing(
                page=page,
                url=url,
                params=common,
                browser_params=browser_params,
            )
            page_content = await page.content()
            page_url = page.url
            title = await page.title()

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    domain = tldextract.extract(page_url).registered_domain

    result = {
        'url': page_url,
        'domain': domain,
        'date': now,
        'title': title,
        'meta': util.social_meta_tags(page_content),
    }

    if common.full_content:
        result['fullContent'] = page_content

    return result


# Create MCP server
server = Server("scrapper")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available scraping tools."""
    return [
        Tool(
            name="scrape_article",
            description=(
                "Extract article content from a web page using a headless browser and Readability.js. "
                "This tool bypasses AI crawler blocks by using a real browser. "
                "Returns the article title, author, content (HTML and text), excerpt, and metadata. "
                "Use this when you need to read the full content of an article that blocks AI crawlers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the article to scrape"
                    },
                    "full_content": {
                        "type": "boolean",
                        "description": "Include the full HTML content of the page",
                        "default": False
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Navigation timeout in milliseconds",
                        "default": 60000
                    },
                    "sleep": {
                        "type": "integer",
                        "description": "Wait time in ms after page load (useful for JS-heavy sites)",
                        "default": 0
                    },
                    "device": {
                        "type": "string",
                        "description": "Device to emulate (e.g., 'Desktop Chrome', 'iPhone 12')",
                        "default": "Desktop Chrome"
                    },
                    "proxy_server": {
                        "type": "string",
                        "description": "Proxy server URL (e.g., http://proxy:3128 or socks5://proxy:1080)"
                    }
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="scrape_links",
            description=(
                "Extract news/article links from a web page (typically a homepage or index page). "
                "Uses a headless browser to bypass AI crawler blocks. "
                "Returns a list of links with their text, filtered to show likely article links. "
                "Use this to discover articles on a news site or blog."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the page to extract links from"
                    },
                    "text_len_threshold": {
                        "type": "integer",
                        "description": "Minimum median text length for link groups",
                        "default": 40
                    },
                    "words_threshold": {
                        "type": "integer",
                        "description": "Minimum median word count for link groups",
                        "default": 3
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Navigation timeout in milliseconds",
                        "default": 60000
                    },
                    "sleep": {
                        "type": "integer",
                        "description": "Wait time in ms after page load",
                        "default": 0
                    },
                    "device": {
                        "type": "string",
                        "description": "Device to emulate",
                        "default": "Desktop Chrome"
                    },
                    "proxy_server": {
                        "type": "string",
                        "description": "Proxy server URL"
                    }
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="fetch_page",
            description=(
                "Fetch a web page using a headless browser without article parsing. "
                "Bypasses AI crawler blocks by using a real browser. "
                "Returns page title and metadata. Use full_content=true to get the HTML. "
                "Use this for pages that aren't articles or when you need raw HTML."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the page to fetch"
                    },
                    "full_content": {
                        "type": "boolean",
                        "description": "Include the full HTML content of the page",
                        "default": True
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Navigation timeout in milliseconds",
                        "default": 60000
                    },
                    "sleep": {
                        "type": "integer",
                        "description": "Wait time in ms after page load",
                        "default": 0
                    },
                    "device": {
                        "type": "string",
                        "description": "Device to emulate",
                        "default": "Desktop Chrome"
                    },
                    "proxy_server": {
                        "type": "string",
                        "description": "Proxy server URL"
                    }
                },
                "required": ["url"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    import json

    url = arguments.get("url")
    if not url:
        return [TextContent(type="text", text="Error: URL is required")]

    try:
        if name == "scrape_article":
            result = await scrape_article_impl(url, arguments)
            # Return a formatted response with the most useful fields
            output = {
                "url": result.get("url"),
                "title": result.get("title"),
                "byline": result.get("byline"),
                "excerpt": result.get("excerpt"),
                "textContent": result.get("textContent"),
                "content": result.get("content"),
                "length": result.get("length"),
                "siteName": result.get("siteName"),
                "publishedTime": result.get("publishedTime"),
                "domain": result.get("domain"),
                "lang": result.get("lang"),
            }
            if arguments.get("full_content"):
                output["fullContent"] = result.get("fullContent")

        elif name == "scrape_links":
            result = await scrape_links_impl(url, arguments)
            output = {
                "url": result.get("url"),
                "title": result.get("title"),
                "domain": result.get("domain"),
                "links": result.get("links", []),
                "link_count": len(result.get("links", [])),
            }

        elif name == "fetch_page":
            result = await fetch_page_impl(url, arguments)
            output = {
                "url": result.get("url"),
                "title": result.get("title"),
                "domain": result.get("domain"),
                "meta": result.get("meta"),
            }
            if arguments.get("full_content", True):
                output["fullContent"] = result.get("fullContent")

        else:
            return [TextContent(type="text", text=f"Error: Unknown tool '{name}'")]

        return [TextContent(type="text", text=json.dumps(output, ensure_ascii=False, indent=2))]

    except (ArticleParsingError, LinksParsingError) as e:
        return [TextContent(type="text", text=f"Parsing error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")]


async def cleanup():
    """Cleanup browser on shutdown."""
    global _browser
    if _browser and _browser.is_connected():
        await _browser.close()


async def _run_server():
    """Run the MCP server (async implementation)."""
    # Ensure user_scripts directory exists
    os.makedirs(settings.USER_SCRIPTS_DIR, exist_ok=True)

    async with stdio_server() as (read_stream, write_stream):
        try:
            await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            await cleanup()


def main():
    """Entry point for the MCP server."""
    asyncio.run(_run_server())


if __name__ == "__main__":
    main()
