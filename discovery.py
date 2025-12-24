import requests
import json
import logging
import time

# Strict user-defined configuration
DISCOVERY_API_URL = "https://market-api.probable.markets/public/api/v1/events"
LIMIT = 100

class DiscoveryService:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
            "Accept": "application/json"
        })

    def discover_markets(self, max_events=None):
        """
        Discovers markets by fetching events from the strict user-defined API.
        Returns a list of market dictionaries with 'title', 'url', 'token_ids', etc.
        """
        logging.info("Starting market discovery via strict API source...")
        discovered_markets = []
        offset = 0
        
        while True:
            try:
                if max_events and len(discovered_markets) >= max_events:
                    break

                # Construct URL with strict parameters
                params = {
                    "closed": "false",
                    "related_tags": "true",
                    "sort": "volume",
                    "order": "desc",
                    "limit": str(LIMIT),
                    "offset": str(offset)
                }
                
                logging.info(f"Fetching events offset={offset} limit={LIMIT}...")
                
                # We use params argument for cleaner URL construction, but requests handles encoding.
                # The user provided a specific string format, but requests params dict is equivalent and safer.
                resp = self.session.get(DISCOVERY_API_URL, params=params, timeout=10)
                resp.raise_for_status()
                events = resp.json()

                if not events:
                    logging.info("No more events returned.")
                    break

                for event in events:
                    if max_events and len(discovered_markets) >= max_events:
                        break

                    # Process each market in the event
                    event_markets = event.get('markets', [])
                    for market in event_markets:
                        try:
                            # Parse clobTokenIds and outcomes (they are JSON strings)
                            clob_token_ids_str = market.get('clobTokenIds', '[]')
                            outcomes_str = market.get('outcomes', '[]')
                            
                            try:
                                clob_token_ids = json.loads(clob_token_ids_str)
                                outcomes = json.loads(outcomes_str)
                            except (json.JSONDecodeError, TypeError):
                                # logging.warning(f"Failed to parse JSON strings for market {market.get('id')}")
                                continue

                            # We need exactly 2 tokens (Yes/No) for the summary format requested
                            if isinstance(clob_token_ids, list) and len(clob_token_ids) == 2 and \
                               isinstance(outcomes, list) and len(outcomes) == 2:
                                
                                # Strict mapping: 
                                # outcomes[0] -> clobTokenIds[0]
                                # outcomes[1] -> clobTokenIds[1]
                                
                                # Determine which is Yes and which is No based on label content
                                # If strict mapping is required without label checking, we just take 0 and 1.
                                # However, for the output "yes_ask" / "no_ask", we need to know which one is Yes.
                                
                                outcome_0 = str(outcomes[0])
                                outcome_1 = str(outcomes[1])
                                token_0 = str(clob_token_ids[0])
                                token_1 = str(clob_token_ids[1])
                                
                                yes_token_id = None
                                no_token_id = None
                                
                                # Heuristic to assign Yes/No for display purposes
                                if outcome_0.lower() == "yes":
                                    yes_token_id = token_0
                                    no_token_id = token_1
                                elif outcome_1.lower() == "yes":
                                    yes_token_id = token_1
                                    no_token_id = token_0
                                else:
                                    # Fallback for non-Yes/No markets (e.g. A vs B)
                                    # We treat 0 as Yes (Left) and 1 as No (Right) for generic display
                                    yes_token_id = token_0
                                    no_token_id = token_1
                                
                                market_info = {
                                    "title": event.get('title'),
                                    "slug": event.get('slug'), # Event slug
                                    "market_slug": market.get('market_slug'), # Market slug
                                    "url": f"https://probable.markets/event/{event.get('slug')}", # Construct URL
                                    "yes_token_id": yes_token_id,
                                    "no_token_id": no_token_id,
                                    "yes_outcome": outcome_0 if outcome_0.lower() == "yes" else outcome_0,
                                    "no_outcome": outcome_1 if outcome_1.lower() == "no" else outcome_1
                                }
                                
                                discovered_markets.append(market_info)
                        except Exception as e:
                            logging.debug(f"Error processing market {market.get('id')}: {e}")

                # Pagination logic
                if len(events) < LIMIT:
                    logging.info("Reached end of list (returned count < limit).")
                    break
                    
                offset += LIMIT
                
            except Exception as e:
                logging.error(f"Discovery failed: {e}")
                break

        logging.info(f"Discovered {len(discovered_markets)} markets.")
        return discovered_markets

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    svc = DiscoveryService()
    markets = svc.discover_markets(max_events=10)
    print(json.dumps(markets, indent=2))
