# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Scrapper is a web scraping service that extracts article content from web pages using a headless Firefox browser (via Playwright) and Mozilla's Readability.js library. It provides a REST API and web interface for parsing articles and collecting news links.

**Key Technologies:**
- FastAPI for REST API and web interface
- Playwright (Firefox) for headless browser automation
- Readability.js for article content extraction
- Python 3.10+ with async/await patterns throughout

## Development Commands

### Local Development
```bash
# Install dependencies (before first run)
pip install -r requirements.txt

# Run development server (port 3001)
make run
# or directly:
uvicorn --app-dir app main:app --port 3001

# Run tests with coverage
make test
# or directly:
./runtest.sh
# Coverage report saved to htmlcov/

# Run linter
make lint
# or directly:
pylint app/*
```

### Docker Development
```bash
# Build Docker image
make docker

# Run in Docker
make docker-run

# Test in Docker
make docker-test

# Lint in Docker
make docker-lint
```

### Testing
Tests are located alongside source files (e.g., [test_main.py](app/test_main.py), [test_errors.py](app/internal/test_errors.py)). Run with:
```bash
PYTHONPATH=$(pwd)/app coverage run --source=app/ -m pytest app/
```

## Architecture

### Application Structure
```
app/
├── main.py                    # FastAPI app, routes mounting, exception handlers
├── dependencies.py            # Lifespan context: Firefox browser + semaphore for context limits
├── settings.py                # Environment config, paths, device registry
├── routers/                   # API endpoint handlers
│   ├── article.py            # /api/article - extract article content
│   ├── links.py              # /api/links - collect news links
│   ├── any_page.py           # /api/any-page - raw page fetching
│   ├── results.py            # /api/results - cached result retrieval
│   ├── misc.py               # /ping, /devices endpoints
│   └── query_params.py       # Pydantic models for all query parameters
├── internal/
│   ├── browser.py            # Core browser automation (contexts, page handling, stealth mode)
│   ├── cache.py              # Disk cache for parsed results
│   ├── errors.py             # Custom exception classes
│   └── util/                 # HTML utilities and helpers
├── scripts/
│   ├── readability/          # Mozilla Readability.js library
│   ├── parser/               # Custom JS parsers (article.js, links.js)
│   └── stealth/              # Anti-detection scripts for headless browser
├── templates/                # Jinja2 HTML templates
└── static/                   # CSS, JS, icons
```

### Core Patterns

**Browser Management:**
- Firefox browser launched at startup via lifespan context in [dependencies.py](app/dependencies.py:19)
- Browser contexts limited by semaphore (default: 20 concurrent contexts via `BROWSER_CONTEXT_LIMIT`)
- Contexts can be incognito or persistent (saves cookies/session to disk)
- All browser operations in [internal/browser.py](app/internal/browser.py)

**Request Flow:**
1. Request hits router (e.g., [article.py](app/routers/article.py))
2. Check cache in [internal/cache.py](app/internal/cache.py)
3. If not cached: create browser context → navigate → inject scripts → extract content
4. Save result to cache and return

**JavaScript Injection:**
- Readability.js injected for article parsing
- Stealth scripts injected when `stealth=true` parameter used
- User-provided scripts from `user_scripts/` directory can be injected via `user-scripts` parameter

**Query Parameters:**
All API parameters defined as Pydantic models in [routers/query_params.py](app/routers/query_params.py):
- `CommonQueryParams`: cache, full-content, stealth, screenshot, user-scripts
- `BrowserQueryParams`: timeout, viewport, device, sleep, incognito, headers
- `ProxyQueryParams`: proxy-server, proxy-username, proxy-password
- `ReadabilityQueryParams`: max-elems-to-parse, nb-top-candidates, char-threshold

### Important Implementation Details

**Device Emulation:**
- Device registry loaded from [internal/deviceDescriptorsSource.json](app/internal/deviceDescriptorsSource.json)
- Default device is "iPhone 12"
- Device settings override individual viewport/user-agent params

**Caching:**
- Results cached in `user_data/_res/` directory (UUID-based filenames)
- Screenshots cached separately
- Cache key based on URL + query parameters hash
- Enable/disable via `cache` parameter (default: true)

**Error Handling:**
- Custom exceptions in [internal/errors.py](app/internal/errors.py)
- Playwright errors caught globally in [main.py](app/main.py:67)
- Detailed error responses in JSON format

**Environment Variables:**
- `USER_DATA_DIR`: Browser session data and cache storage (default: `user_data/`)
- `USER_SCRIPTS_DIR`: Custom JavaScript files (default: `user_scripts/`)
- `BROWSER_CONTEXT_LIMIT`: Max concurrent browser contexts (default: 20)
- `SCREENSHOT_TYPE`: jpeg or png (default: jpeg)
- `SCREENSHOT_QUALITY`: 0-100 (default: 80)

## API Endpoints

- `GET /api/article?url=<url>` - Extract article content from URL
- `GET /api/links?url=<url>` - Collect news links from homepage
- `GET /api/any-page?url=<url>` - Fetch raw page content
- `GET /api/results/<result_id>` - Retrieve cached result
- `GET /ping` - Healthcheck endpoint
- `GET /devices` - List available device emulation profiles
- `GET /` - Web interface for building queries

## Project Conventions

- **Async/Await:** All I/O operations are async (FastAPI, Playwright)
- **Type Hints:** Extensive use of type annotations and Pydantic models
- **Error Messages:** Descriptive exceptions with context for debugging
- **Testing:** Unit tests colocated with source files, pytest with coverage
- **Linting:** Pylint configuration in [.pylintrc](.pylintrc) (target score: 9.95+)
- **Docker:** Primary deployment method, based on official Playwright Python image
- **User Permissions:** Container runs as UID 1001 (not root), host directories must have correct permissions

## Playwright Usage Notes

- **Headless Mode:** Browser always runs headless (this is a web service)
- **macOS Considerations:** This project expects standard Firefox/Chromium, not Safari-specific features
- **Browser Type:** Firefox is the default browser (configured in [dependencies.py](app/dependencies.py:24))
- **Stealth Mode:** Anti-detection scripts in [app/scripts/stealth/](app/scripts/stealth/) modify navigator properties, webdriver flags, etc.

## Security & Safety

- **Never hardcode values:** Configuration via environment variables and query parameters
- **No database operations:** All data stored as files in `user_data/` directory
- **Proxy Support:** HTTP/SOCKS4/SOCKS5 proxies supported for requests
- **No auto-reload:** Code changes require manual restart (do not check processes or reload)
