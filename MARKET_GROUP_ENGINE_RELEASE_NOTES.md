# Market Group Engine v2.2

- Groups related Polymarket outcomes by event slug.
- Sends and records only the strongest candidate from each event family per scan.
- Adds a persistent event-level cooldown so related outcomes do not produce repeated alerts.
- Applies the same grouping to normal PUMP/DIP alerts and AI Opportunity alerts.
- Saves group metadata in AI Memory for future diagnostics and model training.
- Existing database data is preserved; the new table is created automatically.
