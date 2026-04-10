# Rain Protocol Docs Research Notes

## Navigation Structure (from sidebar)
- Rain Builders - SDK Documentation (main)
- Changelog
- Rain SDK:
  - Creating a Market
  - Trading & Positions
  - Disputes & Appeals
  - Liquidity
  - Authentication
  - Account Abstraction
  - WebSockets & Live Data
  - Analytics & History
  - Environments & Configuration
  - API Reference
  - Full Method Reference
- Rain for AI Agents:
  - Built For AI Agents
  - How it Works
  - OpenClaw Skills
  - Example Prompt
- RNG Layer:
  - Introduction to the RNG Layer
  - Installation & Initialization
  - Liquidity Class
  - Allowance Class
  - Recommended Usage Pattern
  - Local Development Setup
  - Versioning Policy

## Key Pages to Visit for Market Monitoring:
1. WebSockets & Live Data - likely has real-time market event subscriptions
2. Analytics & History - may have historical market data endpoints
3. API Reference - REST API endpoints
4. Full Method Reference - all SDK methods
5. Environments & Configuration - API base URLs
6. Creating a Market - understand market structure
7. Authentication - how to authenticate API calls

## Key Finding from Main Page:
- SDK method: `rain.getPublicMarkets({ limit: 10 })` - fetches active markets
- Live Data Streams: "Connect to our WebSockets to pull live AMM pricing, order history, and market resolution events"

## Analytics & History Page
- Requires subgraphUrl when initializing Rain client
- Methods: getPriceHistory, getPnL, getLeaderboard, getTransactionDetails, getTransactions, getMarketTransactions, getTradeHistory
- "standard market queries read directly from the blockchain or the Rain API" - confirms there's a Rain API
- Subgraph is used for indexed historical data


## WebSockets & Live Data Page (CRITICAL)
- RainSocket class wraps socket.io for typed WebSocket connections
- `new RainSocket({ environment: 'production' })` - connects to Rain WebSocket
- Events: onEnterOption, onOrderCreated, DisputeOpenedEventData
- subscribeToMarketEvents for specific market lifecycle events
- subscribePriceUpdates for live AMM price feeds
- WebSocket events are per-market (need marketAddress or marketId)
- NOTE: WebSocket is per-market, not for "new market created" events globally
- This means WebSocket alone won't detect NEW markets - need API polling


## Environments & Configuration Page (CRITICAL - API ENDPOINTS FOUND!)
| Environment | API Endpoint | Factory Address |
|---|---|---|
| development | dev-api.rain.one | 0x148DA7F2039B2B00633AC2a |
| staging | stg-api.rain.one | 0x6109c9f28FE3Ad84c51368f7I |
| production | prod-api.rain.one | 0xccCB3C03D9355B01883779E |

Data Sources: SDK reads from three sources - blockchain, Rain API, and Subgraph

PRODUCTION API: https://prod-api.rain.one


## Data Sources Table (KEY FINDING!)
| Source | Used by | Config |
|---|---|---|
| Rain API | getPublicMarkets, getMarketDetails (partial), buildClaimTx, buildCreateMarketTx | environment (auto) |
| On-chain | getMarketPrices, getMarketVolume, getPositions, all tx builders | rpcUrl (auto-selected) |
| Subgraph | getTransactions, getPriceHistory, getPnL, getLeaderboard | subgraphUrl + subgraphApiKey |

KEY: `getPublicMarkets` uses the Rain API directly! This is the method to poll for new markets.
The API endpoint for production is: prod-api.rain.one


## API Reference Page (CRITICAL - SWAGGER DOCS FOUND!)

### Swagger API Docs:
- Dev: https://dev-api.rain.one/api-docs/
- Staging: https://stg-api.rain.one/api-docs/
- Production: https://api.rain.one/api-docs/

NOTE: Production endpoint is api.rain.one (not prod-api.rain.one as shown in env table!)

### getPublicMarkets(params) - KEY METHOD:
```
const markets = await rain.getPublicMarkets({
  limit: 12,
  offset: 0,
  sortBy: 'Liquidity', // 'Liquidity' | 'Volumn' | 'latest'
  status: 'Live', // 'Live' | 'Trading' | 'Closed' | ...
  creator: '0x...', // Optional
});
// Returns: Market[]
// { id, title, totalVolume, status, contractAddress, poolOwnerWalletAddress }
```

### getMarketDetails(marketId) - DETAILED INFO:
```
// Returns: MarketDetails
// { id, title, status, contractAddress,
//   options: [{ choiceIndex, optionName, currentPrice, totalFunds, totalVotes }],
//   poolState, numberOfOptions, startTime, endTime, oracleEndTime,
//   allFunds, allVotes, totalLiquidity, winner, poolFinalized,
//   isPublic, baseToken, baseTokenDecimals, poolOwner, resolver,
//   resolverIsAI, isDisputed, isAppealed }
```


## Swagger API Docs - /pools/public-pools Endpoint (CRITICAL!)

### Endpoint: GET /pools/public-pools
- Fetches a paginated list of public pools. Supports filtering by tags.

### Parameters:
- limit (integer, query): Number of pools to fetch per page. Default is 10.
- offset (integer, query): Page number for pagination. Default is 1.
- tag (string, query): Filter pools by tag. Matching is case-insensitive.
- sortBy (string, query): Filter pools by sortBy. Default filter is age.
- status (string, query): Filter pools by their status. "open" for ongoing pools, "closed" for finalized pools.
  Available values: New, Live, Waiting_for_Result, Under_Dispute, Under_Appeal, Closing_Soon, Closed

### Response 200 Example:
```json
{
  "statusCode": 200,
  "message": "Public pool list retrieved successfully.",
  "data": {
    "pools": [
      {
        "_id": "67485b984a2f4e38b8c976b7",
        "question": "Who will win the next soccer match?",
        "tags": ["movies"],
        "options": [
          {
            "optionName": "Team A",
            "optionImage": "https://example.com/team-a.jpg"
          }
        ],
        "createdAt": "2024-12-01T00:00:00Z",
        "isPrivate": false
      }
    ],
    "count": 50,
    "pages": 5
  }
}
```

### Server URLs:
- Development: https://dev-api.rain.one
- Staging: https://stg-api.rain.one
- Production: https://prod-api.rain.one

### Strategy for monitoring:
Poll GET /pools/public-pools with sortBy=age (newest first) and status=New or Live
Track seen pool IDs, alert on new ones
For details, use GET /pools/pool/{id}


## Market URL Pattern (CONFIRMED!)
The market detail page URL pattern is:
https://www.rain.one/detail?id={pool_id}

Example: https://www.rain.one/detail?id=69368cf62b28bc923f7100d8

## FINAL STRATEGY:
1. Poll GET https://prod-api.rain.one/pools/public-pools?limit=10&offset=1&sortBy=age periodically
2. Track seen pool IDs in a JSON file
3. For new pools, fetch details from GET https://prod-api.rain.one/pools/pool/{id}
4. Send Telegram notification with:
   - Market Question: pool.question
   - Creator: pool.poolOwnerNameOrWallet or pool.poolOwnerWalletAddress
   - Market ends: pool.endDate
   - Link: https://www.rain.one/detail?id={pool._id}

