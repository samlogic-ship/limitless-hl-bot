import { defineConfig, markdown } from "sourcey";

const commit = "5c569c592082a823f22850722ec3f88d0fb2dc3a";

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
