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

## โหลดชุดใหม่ Lighter XAU/PAXG ที่ดาวน์โหลดจาก 0xArchive (22 พ.ย. 2026)

หลังดาวน์โหลดเสร็จ ใช้ไฟล์ SQLite เต็มใน WSL โดยตรง **ไม่ต้องอัปโหลดไฟล์ ZIP review** และไม่ต้องคัดลอกข้อมูลทั้งหมดลง Windows

```bash
cd ~/code/trading-backtest
source .venv/bin/activate
git pull origin master
python -m pytest -q tests/test_lighter_local_paths.py tests/test_quotes.py
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

เปิด http://127.0.0.1:8501 ด้วยเบราว์เซอร์เอง

1. Sidebar > **Or local file path (read-only)** ใส่ `/home/june-asus/code/trading-backtest/data/lighter_fresh_20261122/lighter_marketdata.sqlite3`
2. Source symbol = XAU; Alias = XAU; กด Load asset
3. Source symbol = PAXG; Alias = PAXG; กด Load asset
4. Historical Bid/Ask > **Or local historical quotes file path** ให้ใส่พาธ SQLite เดียวกัน; เลือก asset XAU และ Source symbol XAU แล้วกด Load historical Bid/Ask
5. เลือก asset PAXG และ Source symbol PAXG; กด Load historical Bid/Ask อีกครั้ง
6. เลื่อนเวลา Replay เพื่อดู Bid, Ask และ top-level volumes ในตาราง หาก quote เก่าเกินกว่าค่า Maximum quote age จะไม่ถือว่าสด

**ข้อจำกัดสำคัญ:** แอป MVP ปัจจุบันแสดง/ใช้เฉพาะ Best Bid/Ask พร้อม Size ชั้นแรกเท่านั้น แม้ไฟล์ดิบจาก 0xArchive จะบันทึก L2 20 ระดับต่อฝั่งก็ตาม การคำนวณ executable VWAP, การจำลองการใช้ Depth ร่วมกันหลายออร์เดอร์ และ Maker queue/fill ต้องพัฒนาเพิ่มก่อนใช้งาน Backtest แบบอัตโนมัติจริงจัง

PAXG 1m candles อาจเป็น sparse candles (บางนาทีไม่มี trade) จึงต้องแยก `NO_TRADE` จาก `NO_DATA` และห้ามเติม OHLC เทียมแล้วอ้างว่าเป็นการซื้อขายจริง

อัตรา funding ในไฟล์ฐานข้อมูลบันทึกถี่ประมาณ 10 วินาที เป็น rate observations ไม่ใช่ cash settlement records จึง **ห้าม** รวมจำนวนรายการเป็นจำนวนครั้งที่จ่าย/รับ Funding

รายงาน `lighter_fresh_review_20261122.zip` มีเพียงตัวอย่างข้อมูลและสถิติ ไม่ใช่ฐานข้อมูลเต็ม
