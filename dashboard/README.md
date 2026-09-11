# DeepFlow Dashboard

Next.js + TypeScript frontend for the DeepFlow trading system.

```bash
npm install
npm run dev     # http://localhost:3000
```

Set `NEXT_PUBLIC_API_BASE` to the DeepFlow API (default `http://localhost:8000`).

## Status

Scaffold. `src/types/api.ts` mirrors `deepflow/api/schemas.py` and is the
contract between the two; keep them in sync. Panels are built in Phase 7 —
see [`../docs/ROADMAP.md`](../docs/ROADMAP.md).

## Notes

The dashboard holds no trading logic and never talks to Polymarket directly.
All state comes from the authenticated DeepFlow API.

Smart-money entry odds are displayed verbatim, never rounded: a whale filling
at 0.887 and one filling at 0.94 are different pieces of evidence.
