import os
import json
import requests
from concurrent.futures import ThreadPoolExecutor


class Search:
    tool_schema = {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Performs web searches and returns the top 10 results for each query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Array of query strings. Include multiple complementary search queries in a single call."
                    },
                },
                "required": ["query"],
            }
        }
    }

    def __init__(self):
        self.api_key = os.getenv("BRAVE_SEARCH_KEY")

    def brave_search(self, query: str):
        url = 'https://api.search.brave.com/res/v1/web/search'
        headers = {
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'X-Subscription-Token': self.api_key,
        }
        params = {
            "q": query,
            "count": 10,
        }

        response = None
        results = None

        for i in range(5):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=10)
                results = response.json()
                break
            except Exception as e:
                print(f"[search] Attempt {i+1}/5 failed: {e}")
                if i == 4:
                    return f"[search] Error: Brave search failed after 5 retries for query '{query}'."

        if response is None:
            return f"[search] Error: Failed to get response for query '{query}'."

        if response.status_code != 200:
            return f"[search] Error: {response.status_code} - {response.text}"

        try:
            web_results = results.get("web", {}).get("results", [])
            if not web_results:
                return f"[search] No results found for query: '{query}'. Use a less specific query."

            web_snippets = []
            for idx, page in enumerate(web_results, 1):
                date_published = f"\nDate published: {page['page_age']}" if "page_age" in page else ""
                snippet = f"\n{page['description']}" if "description" in page else ""
                redacted_version = f"{idx}. [{page['title']}]({page['url']}){date_published}{snippet}"
                web_snippets.append(redacted_version)

            content = f"A Brave search for '{query}' found {len(web_snippets)} results:\n\n## Web Results\n" + "\n\n".join(web_snippets)
            return content

        except Exception as e:
            print(f"[search] Error parsing results for '{query}': {e}")
            return f"[search] Error parsing search results for '{query}'. Try with a more general query."

    async def call(self, params, **kwargs):
        if not self.api_key:
            return "[search] Error: BRAVE_SEARCH_KEY environment variable not set."

        try:
            if isinstance(params, str):
                params = json.loads(params)
            query = params["query"]
        except Exception as e:
            print(f"[search] Parameter parsing error: {e}")
            return "[search] Invalid request format: Input must be a JSON object containing 'query' field"

        if isinstance(query, str):
            response = self.brave_search(query)
        elif isinstance(query, list):
            if len(query) == 0:
                return "[search] Error: query array is empty."
            with ThreadPoolExecutor(max_workers=min(3, len(query))) as executor:
                responses = list(executor.map(self.brave_search, query))
            response = "\n=======\n".join(responses)
        else:
            return f"[search] Error: 'query' must be a string or array, got {type(query).__name__}."

        return response