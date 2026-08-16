AI Memory v2.2

Replace these files:
- ai_engine.py
- memory_engine.py
- bot.py
- config.py

Changes:
- signed result at checkpoint (negative movement is no longer clipped to 0)
- statuses: SUCCESS >= +10%, PARTIAL >= +3%, NEUTRAL between -3% and +3%, FAIL <= -3%
- one-time recalculation of existing outcomes on first launch
- separate PUMP/DIP statistics
- average directional result and continuation rate
- timestamps in audit output
