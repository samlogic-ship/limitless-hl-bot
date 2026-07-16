import { defineConfig, markdown } from "sourcey";

const commit = "d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db";

export default defineConfig({
  name: "Limitless HL Bot Maintainer Reference",
  repo: `https://github.com/samlogic-ship/limitless-hl-bot/tree/${commit}`,
  navigation: {
    tabs: [
      {
        tab: "Limitless HL Bot",
        source: markdown({
          groups: [
            { group: "Start", pages: ["overview"] },
            {
              group: "System",
              pages: [
                "market-discovery",
                "scoring-and-signals",
                "execution-and-orders",
                "risk-and-exits",
                "operations-and-learning"
              ]
            }
          ]
        })
      }
    ]
  }
});
