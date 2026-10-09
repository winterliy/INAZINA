# HTS Chart Drawing Tools

The HTS chart should support cursor/selection, trend lines, and horizontal price levels. Users draw a trend line by dragging and place a horizontal line by clicking a price. Drawings can be selected, moved, deleted, undone, and cleared.

In the real HTS, persist drawing anchors as chart time and price values, not screen pixels. Drawings must stay attached to the same time and price when users resize, zoom, pan, or change candle intervals. Scope saved drawings by authenticated user and instrument.

The current `ui-mockups/hts.html` preview demonstrates the interaction only. It stores normalized chart-area coordinates in browser local storage and does not connect to market data.
