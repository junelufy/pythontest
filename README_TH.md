# วิธีเริ่มใช้ Trading Backtest Workstation (Prototype v0.1)

แอปนี้เป็นเครื่องมือจำลองเทรดย้อนหลังในเครื่อง ไม่ส่งคำสั่งซื้อขายจริง

## ติดตั้งใน WSL อันที่ 2

```bash
mkdir -p ~/code
cd ~/code
git clone https://github.com/junelufy/pythontest.git trading-backtest
cd trading-backtest
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

เปิดเบราว์เซอร์เองไปที่ http://127.0.0.1:8501

## วิธีทดลอง

1. อัปโหลดไฟล์ Lighter หรือ Binance ZIP ที่มี `candles` SQLite
2. เลือกสินทรัพย์ เช่น `XAU` แล้วตั้ง alias; กด **Load asset**
3. เลือก `PAXG` แล้วกด **Load asset** อีกครั้ง
4. ในกราฟ Synthetic ใช้สูตร `XAU - PAXG`
5. เดินเวลาไปยังแท่งที่ต้องการ กด SELL XAU และ BUY PAXG แล้วดู Floating P/L
6. ใช้ Close Position ปิดทั้งไม้หรือบางส่วน
7. Export Scenario JSON เพื่อเก็บผล

## ข้อจำกัดที่ต้องทราบ

- Market ใช้ราคาปิดของแท่งล่าสุดที่แสดงแทนราคา Fill จริง
- Limit ใช้ High/Low ของแท่งถัดไปสมมติการ Fill (ไม่มีคิว Maker)
- ไม่รองรับ Funding, Bid/Ask Depth, Margin, Liquidation, Options หรืออัตราแลกเปลี่ยนต่างสกุล
- ยังไม่มี Play อัตโนมัติและระบบ Scenario Restore; ใช้ Step และ Slider ก่อน
- ไม่ควรใช้ตัวเลขกำไรของรุ่นนี้เพื่อยืนยันว่า Strategy เทรดจริงทำกำไรได้

มีเอกสารส่งต่องานให้ Codex ที่ `AGENTS.md` และ `docs/HANDOFF.md`。


## แผง Bid/Ask และปริมาณ (เพิ่ม 2026-11-21)

- หลังโหลด XAU/PAXG แล้ว เลื่อนไปที่ **Historical Bid / Ask / Liquidity at replay time** (ก่อนส่วน Simulated manual trading)
- ตารางแสดง Best Bid, Bid Volume, Best Ask, Ask Volume, Last Candle Close, High, Low, Candle Traded Volume และสถานะของข้อมูล
- ข้อมูล Lighter 30 วันเดิมมีเพียง OHLC + traded volume ไม่มีประวัติ Bid/Ask หรือ book depth ช่อง Bid/Ask จึงเป็นค่าว่างตามจริง ไม่ใช่ศูนย์
- หากมีไฟล์ราคาจาก Order Book ย้อนหลัง สามารถเพิ่มที่ Sidebar > **Historical Bid / Ask** > Import quotes CSV / ZIP / Parquet / SQLite แล้วเลือก asset และกด Load historical Bid/Ask
- ตัวอย่าง CSV (เวลาต้องเป็น UTC; ขนาด bid_size/ask_size เป็นหน่วยสินทรัพย์ ไม่ใช่ USD):

```csv
ts_ms,symbol,bid,ask,bid_size,ask_size
1793836800000,XAU,4065.00,4065.10,2.5,1.4
1793836860000,XAU,4065.20,4065.30,3.1,0.8
```

**ตัวเลข CSV ข้างต้นเป็นข้อมูลสมมติ ใช้ทดสอบรูปแบบไฟล์เท่านั้น**

- เมื่อ Quote สดตามเกณฑ์ Maximum quote age แอปจำลอง Market Buy ที่ Ask และ Market Sell ที่ Bid
- ถ้าปริมาณที่ระดับ Best Bid/Ask ไม่พอรองรับออร์เดอร์ จะไม่จำลองว่าจับคู่เต็มจำนวนได้ที่ราคานั้น แต่ต้องใช้ข้อมูล Depth มากกว่าหนึ่งระดับในเวอร์ชันถัดไป
- ถ้าข้อมูล Quote ไม่มีหรือเก่าเกินกำหนด แอปจะใช้ราคาปิดแท่งเทียนเป็น **proxy** ตามต้นแบบเดิม พร้อมคำเตือน
- Volume ของแท่งเทียนเป็นปริมาณที่ซื้อขายเสร็จแล้ว ไม่ใช่ Bid Volume หรือ Ask Volume ที่รอจับคู่
- Quote import แบบนี้รองรับ **Best level เท่านั้น** ยังไม่ใช่ Depth หลายราคาและไม่ยืนยันการ Fill ของ Maker

หลังแก้ไขโค้ดบน GitHub ให้หยุด Streamlit (Ctrl+C) จาก WSL อันที่ 2 แล้วรัน `git pull` จาก `~/code/trading-backtest` และเริ่ม Streamlit ใหม่
