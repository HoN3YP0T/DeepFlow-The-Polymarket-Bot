// Dashboard shell. Section 22.
//
// Panels, in the order an operator needs them during an incident:
//   Overview          balance, capital, exposure, P&L, drawdown, status
//   Available Trades  live cards incl. smart-money size and exact entry odds
//   Open Positions    entry, mark, P&L, exit score, event state
//   Whale Dashboard   new whales, smart-money entries AND exits
//   Strategy Controls per-strategy toggles and thresholds
//   Risk Controls     pause, resume, cancel all, close all, emergency stop
//   System Health     SDK, CLOB, WS, Data API, Relayer, DB, Redis, clock
//
// Risk Controls sit above System Health deliberately: during an incident the
// kill switches must not be below the fold.

export default function Home() {
  return (
    <main>
      <h1>DeepFlow</h1>
      <p>Dashboard skeleton — panels are wired in Phase 7 (see docs/ROADMAP.md).</p>
    </main>
  );
}
