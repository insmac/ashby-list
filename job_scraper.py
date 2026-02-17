#!/usr/bin/env python3
"""
Ashby Job Scraper - Uses Playwright for reliable Google scraping
and stores new jobs in Neon PostgreSQL database.

Usage:
    uv run python job_scraper.py

Environment variables required:
    DATABASE_URL - Neon PostgreSQL connection string
    BROWSERLESS_URL - (optional) Browserless.io WebSocket endpoint

Install dependencies:
    uv sync
    uv run playwright install chromium
"""

import os
import re
import hashlib
import logging
import asyncio
from datetime import datetime
from urllib.parse import quote_plus, urlparse, parse_qs
import random

import psycopg2
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
DEFAULT_SEARCH_QUERY = 'site:jobs.ashbyhq.com ("front-end" OR "frontend" OR "fullstack" OR "product") remote'
SEARCH_QUERY = os.getenv('SEARCH_QUERY', DEFAULT_SEARCH_QUERY)
MAX_PAGES = 10  # Max search result pages to scrape

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id SERIAL PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    url_hash VARCHAR(64) UNIQUE NOT NULL,
    title TEXT,
    company TEXT,
    description TEXT,
    search_rank INTEGER,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_jobs_url_hash ON jobs(url_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered_at ON jobs(discovered_at);
CREATE INDEX IF NOT EXISTS idx_jobs_search_rank ON jobs(search_rank);
"""


def get_db_connection():
    """Create database connection from DATABASE_URL env var."""
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        raise ValueError("DATABASE_URL environment variable not set")
    return psycopg2.connect(db_url)


def init_db():
    """Initialize database schema."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(DB_SCHEMA)
        conn.commit()
    logger.info("Database initialized")


def hash_url(url: str) -> str:
    """Create consistent hash for URL deduplication."""
    normalized = url.lower().rstrip('/')
    return hashlib.sha256(normalized.encode()).hexdigest()


def extract_company(url: str, title: str) -> str:
    """Extract company name from Ashby URL or title."""
    match = re.search(r'jobs\.ashbyhq\.com/([^/]+)', url)
    if match:
        company = match.group(1)
        return company.replace('-', ' ').title()
    if ' at ' in title:
        return title.split(' at ')[-1].strip()
    return ''


async def search_google_playwright(query: str) -> list[dict]:
    """Search Google using Playwright with stealth settings."""
    results = []
    
    async with async_playwright() as p:
        # Connect to Browserless.io or launch local browser
        browserless_url = os.getenv('BROWSERLESS_URL')
        chrome_path = os.getenv('CHROME_USER_DATA_DIR')
        context = None
        browser = None
        
        if browserless_url:
            logger.info("Connecting to Browserless...")
            browser = await p.chromium.connect_over_cdp(browserless_url)
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York',
            )
        elif chrome_path and os.path.exists(chrome_path):
            profile = os.getenv('CHROME_PROFILE', 'Default')
            profile_path = os.path.join(chrome_path, profile)
            logger.info(f"Launching Chrome with profile: {profile}")
            # launch_persistent_context returns a context directly
            context = await p.chromium.launch_persistent_context(
                profile_path,
                headless=False,
                channel='chrome',
                viewport={'width': 1920, 'height': 1080},
                args=[
                    '--disable-blink-features=AutomationControlled',
                ],
            )
        else:
            logger.info("Launching local Chromium (headless)...")
            logger.warning("⚠️  May hit CAPTCHA - set CHROME_USER_DATA_DIR for better results")
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                ]
            )
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York',
            )
        
        # Add stealth scripts
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)
        
        page = await context.new_page()
        
        try:
            # Go to Google
            await page.goto('https://www.google.com', wait_until='networkidle')
            await asyncio.sleep(random.uniform(1, 2))
            
            # Handle various consent/cookie popups (covers multiple languages)
            consent_selectors = [
                # English
                'button:has-text("Accept all")',
                'button:has-text("Accept")',
                'button:has-text("I agree")',
                'button:has-text("Agree")',
                'button:has-text("Allow all")',
                'button:has-text("Allow")',
                'button:has-text("Got it")',
                'button:has-text("OK")',
                # Polish
                'button:has-text("Zaakceptuj wszystko")',
                'button:has-text("Akceptuję")',
                'button:has-text("Zgadzam się")',
                # Generic selectors
                'button[id*="accept"]',
                'button[aria-label*="Accept"]',
                'div[role="dialog"] button:first-of-type',
                '.QS5gu.sy4vM',  # Google's consent button class
            ]
            
            for selector in consent_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        logger.info(f"Clicked consent button: {selector}")
                        await asyncio.sleep(1)
                        break
                except PlaywrightTimeout:
                    continue
            
            # Also try pressing Escape to dismiss any overlay
            await page.keyboard.press('Escape')
            await asyncio.sleep(0.5)
            
            # Type search query - use fill() instead of click + type
            search_box = page.locator('textarea[name="q"], input[name="q"]')
            await search_box.fill(query, timeout=5000)
            await asyncio.sleep(0.5)
            
            await page.keyboard.press('Enter')
            await page.wait_for_load_state('networkidle')
            
            # Scrape multiple pages
            for page_num in range(MAX_PAGES):
                logger.info(f"Scraping page {page_num + 1}...")
                await asyncio.sleep(random.uniform(2, 4))
                
                # Extract results from current page
                page_results = await extract_search_results(page)
                results.extend(page_results)
                logger.info(f"Found {len(page_results)} results on page {page_num + 1}")
                
                # Try to go to next page
                try:
                    next_btn = page.locator('a#pnnext')
                    if await next_btn.is_visible(timeout=3000):
                        await next_btn.click()
                        await page.wait_for_load_state('networkidle')
                    else:
                        logger.info("No more pages")
                        break
                except PlaywrightTimeout:
                    logger.info("No next page found")
                    break
                    
        except Exception as e:
            logger.error(f"Error during scraping: {e}")
            # Take screenshot for debugging
            await page.screenshot(path='error_screenshot.png')
            raise
        finally:
            await context.close()
            if browser:
                await browser.close()
    
    return results


async def extract_search_results(page) -> list[dict]:
    """Extract job listings from Google search results page."""
    results = []
    
    # Wait for page to settle
    await asyncio.sleep(2)
    
    # Save screenshot for debugging
    await page.screenshot(path='debug_search.png')
    logger.info("Saved debug screenshot to debug_search.png")
    
    # Also log the current URL
    current_url = page.url
    logger.info(f"Current page URL: {current_url}")
    
    # Get ALL links on the page and filter manually
    all_links = await page.locator('a').all()
    logger.info(f"Total links on page: {len(all_links)}")
    
    for link in all_links:
        try:
            href = await link.get_attribute('href')
            if not href:
                continue
            
            # Check if URL contains ashbyhq (either direct or in Google redirect)
            if 'jobs.ashbyhq.com' not in href:
                continue
            
            logger.info(f"Raw href found: {href[:100]}...")
            
            # Extract real URL from Google redirect
            real_url = href
            if '/url?q=' in href or 'url?q=' in href:
                try:
                    from urllib.parse import unquote
                    start = href.find('url?q=') + 6
                    end = href.find('&', start) if '&' in href[start:] else len(href)
                    real_url = unquote(href[start:end])
                except:
                    pass
            
            # Get all text content from the link element
            full_text = await link.text_content()
            
            # Try to get title from h3 first
            title = ''
            try:
                h3 = link.locator('h3').first
                if await h3.count():
                    title = await h3.text_content()
            except:
                pass
            
            # Fallback: first line of text
            if not title and full_text:
                title = full_text.strip().split('\n')[0]
            
            # Description: rest of the text or parent text
            description = ''
            if full_text:
                lines = full_text.strip().split('\n')
                if len(lines) > 1:
                    description = ' '.join(lines[1:])
            
            # If no description from link, try parent element
            if not description:
                try:
                    parent = link.locator('..')
                    parent_text = await parent.text_content()
                    if parent_text and parent_text != full_text:
                        description = parent_text.replace(full_text, '').strip()[:500]
                except:
                    pass
            
            company = extract_company(real_url, title or '')
            
            logger.info(f"Found job: {title[:50] if title else 'No title'} at {company}")
            
            results.append({
                'url': real_url,
                'title': title.strip() if title else '',
                'company': company,
                'description': description.strip() if description else '',
                'search_rank': len(results) + 1,  # 1-based position
            })
            
        except Exception as e:
            logger.debug(f"Error extracting result: {e}")
            continue
    
    return results


def save_jobs(jobs: list[dict]) -> tuple[int, int]:
    """Save jobs to database. Returns (new_count, total_count)."""
    if not jobs:
        return 0, 0
    
    new_count = 0
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for job in jobs:
                url_hash = hash_url(job['url'])
                try:
                    # Check if exists
                    cur.execute(
                        "SELECT id FROM jobs WHERE url_hash = %s",
                        (url_hash,)
                    )
                    exists = cur.fetchone()
                    
                    if exists:
                        # Update existing - update rank and last_seen
                        cur.execute("""
                            UPDATE jobs SET
                                title = %s,
                                description = %s,
                                search_rank = %s,
                                last_seen_at = CURRENT_TIMESTAMP,
                                is_active = TRUE
                            WHERE url_hash = %s
                        """, (job['title'], job['description'], job['search_rank'], url_hash))
                    else:
                        # Insert new
                        cur.execute("""
                            INSERT INTO jobs (url, url_hash, title, company, description, search_rank)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (
                            job['url'],
                            url_hash,
                            job['title'],
                            job['company'],
                            job['description'],
                            job['search_rank'],
                        ))
                        new_count += 1
                        logger.info(f"✨ New job: {job['title']} at {job['company']} (rank #{job['search_rank']})")
                        
                except psycopg2.Error as e:
                    logger.error(f"Error saving job {job['url']}: {e}")
                    
        conn.commit()
    
    return new_count, len(jobs)


def get_recent_jobs(days: int = 7) -> list[dict]:
    """Get jobs discovered in the last N days."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT url, title, company, description, discovered_at
                FROM jobs
                WHERE discovered_at > NOW() - INTERVAL '%s days'
                  AND is_active = TRUE
                ORDER BY discovered_at DESC
            """, (days,))
            
            columns = ['url', 'title', 'company', 'description', 'discovered_at']
            return [dict(zip(columns, row)) for row in cur.fetchall()]


async def main():
    """Main entry point."""
    logger.info("🚀 Starting job scraper...")
    
    # Initialize database
    init_db()
    
    # Search for jobs
    logger.info(f"🔍 Searching: {SEARCH_QUERY}")
    jobs = await search_google_playwright(SEARCH_QUERY)
    
    # Deduplicate by URL and filter valid Ashby URLs only
    seen = set()
    unique_jobs = []
    rank = 0
    for job in jobs:
        url = job['url']
        if url not in seen and url.startswith('https://jobs.ashbyhq.com'):
            seen.add(url)
            rank += 1
            job['search_rank'] = rank  # Re-assign rank after filtering
            unique_jobs.append(job)
    
    logger.info(f"📋 Found {len(unique_jobs)} unique results")
    
    # Save to database
    new_count, total = save_jobs(unique_jobs)
    logger.info(f"💾 Processed {total} jobs, {new_count} new")
    
    # Show recent jobs
    recent = get_recent_jobs(days=1)
    if recent:
        print(f"\n{'='*60}")
        print(f"Jobs discovered today ({len(recent)}):")
        print('='*60)
        for job in recent:
            print(f"\n📌 {job['title']}")
            print(f"   🏢 {job['company']}")
            if job.get('description'):
                print(f"   📝 {job['description']}")
            print(f"   🔗 {job['url']}")
    
    return new_count


if __name__ == '__main__':
    asyncio.run(main())
