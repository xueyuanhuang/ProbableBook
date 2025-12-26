import argparse
import json
import time
import asyncio
import aiohttp
import logging
import sys
import os
import random
import socket
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any
from decimal import Decimal, getcontext
from discovery import DiscoveryService
from dotenv import load_dotenv, find_dotenv

# Load .env file
load_dotenv(find_dotenv())

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Set decimal precision
getcontext().prec = 10

# Orderbook API Endpoint
# Using api.probable.markets as requested by user.
# Verified that public/api/v1/book?token_id=... works.
ORDERBOOK_API_BASE = "https://api.probable.markets/public/api/v1/book"

def parse_args():
    parser = argparse.ArgumentParser(description="Probable Markets Real-time Orderbook Fetcher")
    parser.add_argument("--all", action="store_true", default=True, help="Automatically discover and fetch all markets (default: True)")
    parser.add_argument("--once", action="store_true", help="Run only once and exit")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds (default: 60)")
    parser.add_argument("--out", help="Output JSONL file path (optional)")
    parser.add_argument("--pretty", action="store_true", help="Print human-readable summary to stdout")
    parser.add_argument("--max-events", type=int, help="Max number of events to fetch (debug)")
    parser.add_argument("--max-markets", type=int, help="Max number of markets to fetch (debug)")
    
    # Telegram Alert Configuration
    parser.add_argument("--alert-sum-threshold", type=float, help="Sum threshold to trigger Telegram alert (e.g. 1.0)")
    parser.add_argument("--tg-token", help="Telegram Bot Token (overrides TG_BOT_TOKEN env var)")
    parser.add_argument("--tg-chat-id", help="Telegram Chat ID (overrides TG_CHAT_ID env var)")
    
    # Watch Mode Arguments
    parser.add_argument("--list-markets", action="store_true", help="List all discovered markets with indices and exit")
    parser.add_argument("--watch-index", type=int, help="Index of the market to watch (from --list-markets)")
    parser.add_argument("--side", choices=["YES", "NO"], type=str.upper, help="Side to watch (YES or NO)")
    parser.add_argument("--trigger-price", type=float, help="Price threshold to trigger alert")
    parser.add_argument("--trigger-op", choices=[">=", ">", "<=", "<"], default=">=", help="Trigger operator (default: >=)")
    parser.add_argument("--alert-cooldown", type=int, default=300, help="Seconds between alerts in watch mode (default: 300, 0=always)")
    
    parser.add_argument("--test-telegram", action="store_true", help="Send a test Telegram message and exit")
    return parser.parse_args()

async def send_telegram_alert(token, chat_id, message):
    if not token or not chat_id:
        logger.warning("Telegram token or chat_id missing, cannot send alert.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    # 1. Detect Proxy
    proxy = (
        os.getenv("HTTPS_PROXY") 
        or os.getenv("https_proxy") 
        or os.getenv("HTTP_PROXY") 
        or os.getenv("http_proxy")
    )
    logger.info(f"Telegram Proxy detected: {bool(proxy)}")
    if proxy:
        logger.debug(f"Using proxy: {proxy}")

    # 2. Force IPv4 to avoid HappyEyeballs/IPv6 issues
    connector = aiohttp.TCPConnector(family=socket.AF_INET)

    try:
        # 3. Create Session with trust_env=True and custom connector
        async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
            # 4. Pass proxy explicitly (safest approach)
            async with session.post(
                url, 
                json=payload, 
                proxy=proxy, 
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"Failed to send Telegram alert: Status={resp.status} Response={text}")
                else:
                    logger.info("Telegram alert sent successfully.")
    except Exception as e:
        logger.exception("Exception sending Telegram alert")

async def test_telegram_mode(args):
    """
    Test Telegram connectivity by sending a simple message.
    """
    tg_token = args.tg_token or os.environ.get("TG_BOT_TOKEN")
    tg_chat_id = args.tg_chat_id or os.environ.get("TG_CHAT_ID")
    
    print(f"DEBUG: TG_BOT_TOKEN detected: {bool(tg_token)}")
    print(f"DEBUG: TG_CHAT_ID detected: {bool(tg_chat_id)}")
    
    if not tg_token or not tg_chat_id:
        logger.error("Cannot run test: Missing TG_BOT_TOKEN or TG_CHAT_ID.")
        return

    logger.info("Sending test message to Telegram...")
    msg = "ProbableBook Telegram test message"
    await send_telegram_alert(tg_token, tg_chat_id, msg)


async def fetch_book(session: aiohttp.ClientSession, token_id: str, max_retries=3) -> dict:
    """
    Fetches the orderbook for a given token ID with retries and jitter.
    """
    url = f"{ORDERBOOK_API_BASE}?token_id={token_id}"
    delay = 0.5
    
    for attempt in range(max_retries):
        try:
            # Jitter
            await asyncio.sleep(random.uniform(0.05, 0.15))
            
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    logger.warning(f"Rate limit hit for token {token_id}, retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.warning(f"Error fetching token {token_id}: status {resp.status}")
                    return {}
        except Exception as e:
            logger.warning(f"Exception fetching token {token_id}: {e}")
            await asyncio.sleep(delay)
            delay *= 2
            
    return {}

def get_best_ask_details(book: dict) -> Optional[tuple[Decimal, Decimal, list]]:
    """
    Extracts the best ask price and its aggregated size from the orderbook.
    Returns (price, aggregated_size, raw_entries) tuple or None.
    """
    if not book or 'asks' not in book:
        return None
    
    orders = book['asks']
    if not orders:
        return None
    
    # Sort asks: ascending (lowest price first)
    # Each order has 'price' and 'size'
    try:
        # Parse all orders to (price, size, raw_order)
        parsed_orders = []
        for o in orders:
            p = Decimal(str(o['price']))
            s = Decimal(str(o['size']))
            parsed_orders.append((p, s, o))
            
        if not parsed_orders:
            return None

        # Sort based on price
        parsed_orders.sort(key=lambda x: x[0])
        
        best_price = parsed_orders[0][0]
        
        # Filter orders at best price level and aggregate size
        best_level_orders = [x for x in parsed_orders if x[0] == best_price]
        aggregated_size = sum(x[1] for x in best_level_orders)
        
        raw_entries = [x[2] for x in best_level_orders]
        
        return best_price, aggregated_size, raw_entries
    except Exception:
        return None

def get_best_price(book: dict, side: str) -> Optional[Decimal]:
    """
    Extracts the best price from the orderbook.
    side: 'bids' or 'asks'
    For 'asks', we want the lowest price (best ask).
    For 'bids', we want the highest price (best bid).
    """
    if not book or side not in book:
        return None
    
    orders = book[side]
    if not orders:
        return None
    
    # Sort orders
    # bids: descending (highest first)
    # asks: ascending (lowest first)
    prices = [Decimal(str(o['price'])) for o in orders]
    if not prices:
        return None
        
    if side == 'bids':
        return max(prices)
    else:
        return min(prices)

def get_best_bid_details(book: dict) -> Optional[tuple[Decimal, Decimal, list]]:
    """
    Extracts the best bid (BUY1) price and its aggregated size from the orderbook.
    Returns (price, aggregated_size, raw_entries) tuple or None.
    Bids are sorted descending (highest price first).
    """
    if not book or 'bids' not in book:
        return None
    
    orders = book['bids']
    if not orders:
        return None
    
    try:
        # Parse all orders to (price, size, raw_order)
        parsed_orders = []
        for o in orders:
            p = Decimal(str(o['price']))
            s = Decimal(str(o['size']))
            parsed_orders.append((p, s, o))
            
        if not parsed_orders:
            return None

        # Sort based on price DESCENDING for Bids
        parsed_orders.sort(key=lambda x: x[0], reverse=True)
        
        best_price = parsed_orders[0][0]
        
        # Filter orders at best price level and aggregate size
        best_level_orders = [x for x in parsed_orders if x[0] == best_price]
        aggregated_size = sum(x[1] for x in best_level_orders)
        
        raw_entries = [x[2] for x in best_level_orders]
        
        return best_price, aggregated_size, raw_entries
    except Exception:
        return None

def sort_markets_deterministically(markets: list) -> list:
    """
    Sorts markets by title, then market_slug to ensure stable indexing.
    """
    return sorted(markets, key=lambda m: (m.get('title', ''), m.get('market_slug', '')))

def discover_and_sort_markets(args) -> list:
    """
    Helper to discover and sort markets.
    """
    discovery = DiscoveryService()
    limit = args.max_events or args.max_markets
    markets = discovery.discover_markets(max_events=limit)
    return sort_markets_deterministically(markets)

def print_market_list(markets: list):
    """
    Prints a numbered list of markets.
    """
    print(f"\nDiscovered {len(markets)} markets:\n")
    print(f"{'IDX':<5} | {'TITLE':<50} | {'SLUG'}")
    print("-" * 100)
    for idx, m in enumerate(markets):
        title = (m.get('title') or "")[:48]
        slug = (m.get('market_slug') or "")
        print(f"{idx:<5} | {title:<50} | {slug}")
    print("\n")

def check_trigger(current_price: float, op: str, trigger_price: float) -> bool:
    if op == ">=":
        return current_price >= trigger_price
    elif op == ">":
        return current_price > trigger_price
    elif op == "<=":
        return current_price <= trigger_price
    elif op == "<":
        return current_price < trigger_price
    return False

async def run_watch_mode(args):
    # 1. Discovery
    markets = discover_and_sort_markets(args)
    if not markets:
        logger.error("No markets found.")
        sys.exit(1)
        
    # 2. Validation
    if args.watch_index < 0 or args.watch_index >= len(markets):
        logger.error(f"Invalid index {args.watch_index}. Valid range: 0-{len(markets)-1}")
        logger.info("Run --list-markets to see available markets.")
        sys.exit(1)
        
    if not args.side or not args.trigger_price:
        logger.error("--side and --trigger-price are required for watch mode.")
        sys.exit(1)
        
    target_market = markets[args.watch_index]
    logger.info(f"WATCH MODE STARTED")
    logger.info(f"Market: {target_market['title']}")
    logger.info(f"ID:     {target_market['market_slug']}")
    logger.info(f"Side:   {args.side}")
    logger.info(f"Trigger: BUY1 {args.trigger_op} {args.trigger_price}")
    
    token_id = target_market['yes_token_id'] if args.side == 'YES' else target_market['no_token_id']
    
    last_alert_ts = 0
    
    while True:
        start_time = time.time()
        
        async with aiohttp.ClientSession() as session:
            book = await fetch_book(session, token_id)
            
            # Extract Buy1
            details = get_best_bid_details(book)
            
            status = "NA"
            buy1_price = 0.0
            size = 0.0
            notional = 0.0
            diff = 0.0
            triggered = False
            
            if details:
                buy1_price = float(details[0])
                size = float(details[1])
                notional = buy1_price * size
                diff = buy1_price - args.trigger_price
                
                # Check Trigger
                triggered = check_trigger(buy1_price, args.trigger_op, args.trigger_price)
                status = "TRIGGERED" if triggered else "OK"
            
            # Output
            ts = datetime.now().strftime('%H:%M:%S')
            
            if args.pretty:
                # Table Row
                # Time | Status | Price | Diff | Notional
                color = "\033[91m" if triggered else "\033[92m" # Red if triggered, Green if OK
                reset = "\033[0m"
                print(f"[{ts}] {color}{status:<10}{reset} | Price: {buy1_price:.4f} | Diff: {diff:+.4f} | Notional: ${notional:.2f}")
            else:
                logger.info(f"[{ts}] Status={status} Price={buy1_price:.4f} Diff={diff:+.4f} Notional=${notional:.2f}")
                
            # Alert Logic
            if triggered:
                now = time.time()
                should_alert = False
                
                if args.alert_cooldown == 0:
                    should_alert = True
                elif (now - last_alert_ts) >= args.alert_cooldown:
                    should_alert = True
                    
                if should_alert:
                    # Send TG
                    tg_token = args.tg_token or os.environ.get("TG_BOT_TOKEN")
                    tg_chat_id = args.tg_chat_id or os.environ.get("TG_CHAT_ID")
                    
                    if tg_token and tg_chat_id:
                        msg = f"🚨 *Probable Market Watch*\n"
                        msg += f"Market: {target_market['title']}\n"
                        msg += f"Side: {args.side}\n"
                        msg += f"Trigger: {buy1_price:.4f} {args.trigger_op} {args.trigger_price}\n"
                        msg += f"Notional: ${notional:.2f}\n"
                        msg += f"URL: {target_market['url']}"
                        
                        logger.info("Sending Telegram Alert...")
                        await send_telegram_alert(tg_token, tg_chat_id, msg)
                        last_alert_ts = now
                    else:
                        logger.warning("Triggered but TG not configured.")

        if args.once:
            break
            
        elapsed = time.time() - start_time
        sleep_time = max(0, args.interval - elapsed)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

async def process_market(session: aiohttp.ClientSession, market: dict) -> Optional[dict]:
    """
    Fetches orderbooks for Yes and No tokens and computes metrics.
    """
    yes_id = market.get('yes_token_id')
    no_id = market.get('no_token_id')
    
    if not yes_id or not no_id:
        return None
        
    # Fetch both concurrently
    task_yes = fetch_book(session, yes_id)
    task_no = fetch_book(session, no_id)
    
    book_yes, book_no = await asyncio.gather(task_yes, task_no)
    
    # Extract Best Ask Details (price, aggregated_size, raw_entries)
    yes_details = get_best_ask_details(book_yes)
    no_details = get_best_ask_details(book_no)
    
    yes_ask = yes_details[0] if yes_details else None
    no_ask = no_details[0] if no_details else None
    
    yes_ask_size = yes_details[1] if yes_details else None
    no_ask_size = no_details[1] if no_details else None
    
    yes_raw = yes_details[2] if yes_details else []
    no_raw = no_details[2] if no_details else []
    
    # Calculate notional USD
    yes_ask_notional = (yes_ask * yes_ask_size) if yes_ask is not None and yes_ask_size is not None else None
    no_ask_notional = (no_ask * no_ask_size) if no_ask is not None and no_ask_size is not None else None
    
    # Calculate sum_flag (sum of best asks)
    sum_flag = None
    both_ask_notional = None
    
    if yes_ask is not None and no_ask is not None:
        sum_flag = yes_ask + no_ask
        if yes_ask_notional is not None and no_ask_notional is not None:
            both_ask_notional = yes_ask_notional + no_ask_notional
        
    return {
        "title": market.get('title'),
        "url": market.get('url'),
        "market_slug": market.get('market_slug') or "unknown-slug",
        "yes_outcome": market.get('yes_outcome'),
        "no_outcome": market.get('no_outcome'),
        "yes_ask": float(yes_ask) if yes_ask is not None else None,
        "no_ask": float(no_ask) if no_ask is not None else None,
        "sum_flag": float(sum_flag) if sum_flag is not None else None,
        "yes_ask_notional_usd": float(yes_ask_notional) if yes_ask_notional is not None else None,
        "no_ask_notional_usd": float(no_ask_notional) if no_ask_notional is not None else None,
        "both_ask_notional_usd": float(both_ask_notional) if both_ask_notional is not None else None,
        "yes_debug_size": float(yes_ask_size) if yes_ask_size is not None else None,
        "no_debug_size": float(no_ask_size) if no_ask_size is not None else None,
        "yes_raw_entries": yes_raw,
        "no_raw_entries": no_raw,
        "timestamp": datetime.now().isoformat() + "Z"
    }

async def run_fetcher(args):
    # 1. Discovery Phase
    discovery = DiscoveryService()
    # Respect limits
    limit = args.max_events or args.max_markets # Simplify: pass limit to discovery
    markets = discovery.discover_markets(max_events=limit)
    
    if not markets:
        logger.error("No markets discovered. Exiting.")
        return

    logger.info(f"Starting fetch loop for {len(markets)} markets...")
    
    # 2. Fetch Loop
    while True:
        start_time = time.time()
        results = []
        
        async with aiohttp.ClientSession() as session:
            tasks = []
            for market in markets:
                tasks.append(process_market(session, market))
            
            # Execute in batches to avoid overwhelming the server/client
            batch_size = 10
            for i in range(0, len(tasks), batch_size):
                batch = tasks[i:i+batch_size]
                batch_results = await asyncio.gather(*batch)
                results.extend([r for r in batch_results if r])
        
        # Calculate Best Market (Common)
        best_market = None
        valid_markets = [r for r in results if r['sum_flag'] is not None]
        if valid_markets:
            best_market = min(valid_markets, key=lambda x: x['sum_flag'])

        # 3. Output
        if args.pretty:
            # Find and print best market opportunity
            if best_market:
                
                print("\n---------------- Best Opportunity ----------------")
                print(f"Market: {best_market['market_slug']}")
                print(f"URL: {best_market['url']}")
                
                y_lbl = best_market.get('yes_outcome') or "Yes"
                n_lbl = best_market.get('no_outcome') or "No"
                y_ask = f"{best_market['yes_ask']:.4f}"
                n_ask = f"{best_market['no_ask']:.4f}"
                
                yes_not_val = best_market['yes_ask_notional_usd']
                no_not_val = best_market['no_ask_notional_usd']
                
                y_not = f"{yes_not_val:.2f}" if yes_not_val is not None else "N/A"
                n_not = f"{no_not_val:.2f}" if no_not_val is not None else "N/A"
                
                sum_val = f"{best_market['sum_flag']:.4f}"
                
                # Calculate executable notional
                executable_notional = 0.0
                if yes_not_val is not None and no_not_val is not None:
                    executable_notional = min(yes_not_val, no_not_val)
                exec_not_str = f"{executable_notional:.2f}"
                
                # Determine sum_flag label
                sum_float = best_market['sum_flag']
                flag_lbl = "EQ1"
                if sum_float > 1.0001: flag_lbl = "GT1"
                elif sum_float < 0.9999: flag_lbl = "LT1"
                
                print(f"Yes: {y_lbl} @ {y_ask} | ${y_not}")
                print(f"No:  {n_lbl}  @ {n_ask}  | ${n_not}")
                print(f"Sum: {sum_val} ({flag_lbl})")
                print(f"Executable USD: ${exec_not_str}")
                print("--------------------------------------------------\n")
            
        if args.out:
            # Append to JSONL
            mode = 'a' if os.path.exists(args.out) else 'w'
            with open(args.out, mode) as f:
                for r in results:
                    f.write(json.dumps(r) + "\n")
                
                # Write best market entry if available
                if best_market:
                    
                    # Determine sum_flag label
                    sum_float = best_market['sum_flag']
                    flag_lbl = "EQ1"
                    if sum_float > 1.0001: flag_lbl = "GT1"
                    elif sum_float < 0.9999: flag_lbl = "LT1"
                    
                    yes_not_val = best_market['yes_ask_notional_usd']
                    no_not_val = best_market['no_ask_notional_usd']
                    executable_notional = 0.0
                    if yes_not_val is not None and no_not_val is not None:
                        executable_notional = min(yes_not_val, no_not_val)
                    
                    best_entry = {
                        "type": "best_market",
                        "market_slug": best_market['market_slug'],
                        "url": best_market['url'],
                        "label_yes": best_market.get('yes_outcome') or "Yes",
                        "label_no": best_market.get('no_outcome') or "No",
                        "yes_ask": best_market['yes_ask'],
                        "no_ask": best_market['no_ask'],
                        "sum": best_market['sum_flag'],
                        "sum_flag": flag_lbl,
                        "yes_ask_notional_usd": yes_not_val,
                        "no_ask_notional_usd": no_not_val,
                        "executable_notional_usd": executable_notional
                    }
                    f.write(json.dumps(best_entry) + "\n")
                    
            logger.info(f"Wrote {len(results)} records + best market to {args.out}")

        # 4. Telegram Alert
        if args.alert_sum_threshold is not None and best_market:
            # Check condition: sum < threshold
            current_sum = best_market['sum_flag']
            if current_sum < args.alert_sum_threshold:
                # Priority: CLI > Env > .env (already loaded into Env)
                tg_token = args.tg_token or os.environ.get("TG_BOT_TOKEN")
                tg_chat_id = args.tg_chat_id or os.environ.get("TG_CHAT_ID")
                
                if not tg_token or not tg_chat_id:
                    logger.warning("Telegram alert threshold met, but TG_BOT_TOKEN or TG_CHAT_ID is missing. Skipping alert.")
                else:
                    logger.info(f"Alert sent (sum {current_sum:.4f} < {args.alert_sum_threshold})")
                    
                    # Prepare message
                    y_lbl = best_market.get('yes_outcome') or "Yes"
                    n_lbl = best_market.get('no_outcome') or "No"
                    y_ask = f"{best_market['yes_ask']:.4f}"
                    n_ask = f"{best_market['no_ask']:.4f}"
                    
                    yes_not_val = best_market['yes_ask_notional_usd']
                    no_not_val = best_market['no_ask_notional_usd']
                    y_not = f"{yes_not_val:.2f}" if yes_not_val is not None else "N/A"
                    n_not = f"{no_not_val:.2f}" if no_not_val is not None else "N/A"
                    
                    sum_val = f"{best_market['sum_flag']:.4f}"
                    
                    sum_float = best_market['sum_flag']
                    flag_lbl = "EQ1"
                    if sum_float > 1.0001: flag_lbl = "GT1"
                    elif sum_float < 0.9999: flag_lbl = "LT1"
                    
                    executable_notional = 0.0
                    if yes_not_val is not None and no_not_val is not None:
                        executable_notional = min(yes_not_val, no_not_val)
                    exec_not_str = f"{executable_notional:.2f}"
                    
                    msg = f"🚨 *Probable Market Alert*\n"
                    msg += f"Market: {best_market['market_slug']}\n"
                    msg += f"Sum: {sum_val} ({flag_lbl})\n"
                    msg += f"Executable USD: ${exec_not_str}\n\n"
                    msg += f"Yes: {y_lbl} @ {y_ask} | ${y_not}\n"
                    msg += f"No:  {n_lbl}  @ {n_ask}  | ${n_not}\n\n"
                    msg += f"URL:\n{best_market['url']}"
                    
                    await send_telegram_alert(tg_token, tg_chat_id, msg)
            else:
                logger.info(f"Alert skipped (sum {current_sum:.4f} >= {args.alert_sum_threshold})")

        if args.once:
            break
            
        # Sleep
        elapsed = time.time() - start_time
        sleep_time = max(0, args.interval - elapsed)
        if sleep_time > 0:
            logger.info(f"Sleeping for {sleep_time:.2f}s...")
            await asyncio.sleep(sleep_time)

def main():
    if sys.prefix == sys.base_prefix:
        logger.error("ProbableBook must be run inside a Python virtual environment (venv).")
        logger.error("Hint: source venv/bin/activate")
        sys.exit(1)
    
    args = parse_args()
    
    # Debug info at startup
    tg_token = args.tg_token or os.environ.get("TG_BOT_TOKEN")
    tg_chat_id = args.tg_chat_id or os.environ.get("TG_CHAT_ID")
    logger.info(f"Configuration: TG_BOT_TOKEN detected: {bool(tg_token)}, TG_CHAT_ID detected: {bool(tg_chat_id)}, Alert Threshold: {args.alert_sum_threshold}")

    # 1. List Markets Mode
    if args.list_markets:
        markets = discover_and_sort_markets(args)
        if not markets:
            logger.error("No markets found.")
            return

        print_market_list(markets)
        return

    # 2. Test Telegram Mode
    if args.test_telegram:
        asyncio.run(test_telegram_mode(args))
        return

    # 3. Execution Mode (Watch vs Scan)
    try:
        if args.watch_index is not None:
            # Watch Mode
            asyncio.run(run_watch_mode(args))
        else:
            # Scan All Mode (Original)
            asyncio.run(run_fetcher(args))
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
