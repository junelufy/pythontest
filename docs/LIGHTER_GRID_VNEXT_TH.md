# Lighter XAU/PAXG — Grid vNext, 26 พ.ย. 2026

## จุดประสงค์
โค้ดชุดนี้เพิ่ม **Buffer $1**, **ยืนยันสัญญาณต่อเนื่อง 10 วินาที**, **จำลอง Post/Cancel ACK**, **จำกัด Pending Maker**, และ **TP ที่คำนวณจากราคา Fill จริงของสองขา** ใน Offline Historical Backtest โดย **ไม่ส่งออร์เดอร์จริง** และไม่เชื่อม WebSocket สด

## ตัวอย่างที่ผู้ใช้ยืนยัน
- Short Grid `+3` ยืน `spread >= +3` ต่อเนื่อง 10 วินาที → ส่ง `BUY LIMIT PAXG` Post-only ที่ Best Bid
- Limit ยังไม่ Fill และ Spread อยู่ `+2 ถึงต่ำกว่า +3` → **คงคำสั่งเดิม**, ไม่ Reprice และไม่ Cancel
- `Spread < +2` → เริ่มส่ง Cancel; Maker ยังมีโอกาส Fill ระหว่าง Cancel Pending
- คำสั่ง Fill (แม้ได้ actual spread = `+2.5`) ถือว่า **Grid +3 ใช้แล้ว** ไม่ย้ายไป Grid +2
- XAU `SELL MARKET` Hedge ใน Live ต้องส่งทันทีเมื่อทราบ Fill; Backtest บันทึก XAU Fill จาก Book แรกที่เวลา `PAXG fill + 2000ms` หรือหลังจากนั้น
- TP $4: ใช้ `actual entry spread +2.5 - 4 = -1.5` เป็นเป้าหมายปิด Short (ไม่ใช้ Grid +3 - 4 = -1.0)
- Grid +4, +5 ยังแยกอิสระจาก Grid +3 ที่ใช้แล้ว
- เมื่อทุก Lot ของ Grid ปิดแล้ว Backtest **สมมติ** ให้เปิด Grid เดิมรอบใหม่ได้ต่อเมื่อ Spread กลับผ่าน Modal Center ก่อน (ตัวเลือกอีกรุ่น `REARM_AFTER_LEAVING_GRID=1`). การ Reset นี้เป็นข้อกำหนดการจำลองที่ยังไม่ยืนยันให้เป็นกฎ Live
- สำหรับ LONG ใช้กฎกลับทิศ: Spread <= G ต่อเนื่อง 10s, `SELL LIMIT PAXG`, Cancel เมื่อ Spread > G+1, XAU BUY Hedge, TP target = actual_entry + TP_distance

## ค่าที่ล็อกโดยผู้ใช้ / ค่าวิจัยที่ยังต้องวัด

| ชื่อ | ค่า | สถานะ |
|---|---|---|
| Modal center | Histogram bin ถี่สุดจาก 7 วันก่อน Test | ยืนยัน |
| Entry dwell | 10 วินาทีต่อเนื่องจากข้อมูลสด | ยืนยันสำหรับรอบทดสอบ |
| Cancel buffer | 1 USD/oz | ยืนยันสำหรับรอบทดสอบ |
| PAXG Maker-first | YES, XAU Taker Hedge | ยืนยัน |
| Hedge latency Backtest | 2000ms | ยืนยัน |
| TP | $2,3,4,5,6,7 ต่อออนซ์จาก actual fill spread | ยืนยันหลักการ |
| Histogram bin | $1 | ค่าวิจัยเดิม |
| Grid step | $1 | ค่าวิจัยเดิม |
| Post ACK / Cancel ACK | 1000ms / 1000ms | **สมมติฐาน** ไม่ได้วัดจาก API จริง |
| Pending cap รวม | 2 | **สมมติฐาน** ควรทดสอบ 1/2/3 |
| Create budget / Cancel budget | 20 / 30 ต่อ 60s | **ตัวจำลอง** ไม่ใช่ Rate Limit ของ Lighter |
| Exposure guardrail | 6 oz ที่เปิดหรือยัง Pending | **สมมติฐาน** ยังไม่ใช่นโยบายจัดการ Margin จริง |
| Quote freshness | 3 วินาที | สมมติฐาน |
| Grid reset | ต้องกลับ Modal Center ก่อนเปิด Grid เดิมใหม่ | สมมติฐานกัน Reuse ง่ายเกินไป |

## ข้อจำกัดการจำลอง
1. `POST_ACK`, `CANCEL_ACK` และ `maker_fill` คือ **เหตุการณ์สมมติ** ที่อนุมานจากข้อมูล Public L2+Trade Tape ไม่ใช่คำยืนยันจาก Matching Engine
2. ราคา Print ที่ทะลุ Limit เป็น `Price-through paper fill` ตามแบบจำลอง Optimistic ซึ่งอาจประมาณ Fill มากเกินไปเมื่อไม่ได้ใส่คำสั่งจริงลงไปใน Book; ตั้ง `STRICT_EXACT_ONLY=1` สำหรับการทดลองเข้มงวด (จะไม่นับ Price-through Fill)
3. Partial Fill: ตัวจำลอง Hedge ตามแต่ละ Lot และเริ่ม Cancel ส่วนที่เหลือทันที; Fill ที่เกิดระหว่างรอ Cancel ยังคงเกิดขึ้นและ Hedge เพิ่มตามจำนวนใหม่
4. Close TP ยังจำลองปิดสองขาพร้อมกันด้วย VWAP ที่ Book ปัจจุบัน ไม่มี Exit Order ACK, Exit Latency หรือ Failover ฉะนั้น **ห้ามใช้ส่งออร์เดอร์เงินจริง**
5. ใช้ Funding Cache รายชั่วโมงจากรอบ TP4–TP7 ล่าสุด ถ้าขาดหายจะรายงาน `UNVERIFIED` ไม่แทนเป็น 0
6. Max Drawdown เป็นการ Mark Fee-free ทุก 60 วินาที ไม่ใช่ Real-Time Liquidation DD
7. ช่วง Test 11–19 พ.ย. 2026 ถูกวิเคราะห์มาแล้ว ไม่ใช่ Unseen Forward Test ใหม่
8. ข้อมูล 1s มี Invalid/Crossed Books และบางช่วงขาดหาย; ตัวจำลองไม่ใช้ Quote เก่าต่อเพื่อยืนยันสัญญาณ

## วิธีรัน (WSL หน้าต่างที่ 2)

หลังดาวน์โหลดและแตก ZIP โค้ดนี้ลง `~/code/trading-backtest` แล้ว:

```bash
bash scripts/run_lighter_grid_vnext_1s_wsl.sh
```

ผล ZIP จะถูกสร้างโดยอัตโนมัติที่ `C:\Users\June-ASUS\Downloads\LIGHTER_GRID_VNEXT_BUFFER1_DWELL10_HEDGE2S_<UTC>.zip` และเก็บรายงานต้นฉบับใน WSL ในโฟลเดอร์แยกตามเวลาที่รัน ไม่เขียนทับผล Backtest TP เดิม

สำหรับ Sensitivity ที่ **ไม่ให้นับ Paper Price-through Fill** (เป็นการทดลองแยก):

```bash
STRICT_EXACT_ONLY=1 bash scripts/run_lighter_grid_vnext_1s_wsl.sh
```

## สิ่งที่ต้องตรวจจากรายงาน
`scenarios.csv` จำนวน 36 แถว (0.5, 1.0 oz × TP 2–7 × 3 Fee Tiers) ตรวจ Maker Posts/Cancel ACKs, Maker Fill Events, Fills During Cancel, TP Closes, Funding, Fee-only Net, Full MTM, Max Exposure, Stale/Invalid Books และ Drawdown

`closed_trades.csv` ต้องมี `grid_level_usd_oz`, `actual_entry_spread_usd_oz`, `target_exit_spread_usd_oz`, `actual_exit_spread_usd_oz`, `slippage_vs_grid_usd_oz` และราคา XAU/PAXG เข้า–ออกจริงในแบบจำลอง สำหรับตัวอย่าง Grid3, Entry2.5, TP4 ค่าต้องเป็น Grid3 / Entry2.5 / Target−1.5 ทุกครั้ง

`order_event_audit.csv` บันทึกได้สำหรับ Scenario แรก (0.5 oz TP2) เพื่อไม่สร้าง Log ขนาดมหาศาลจากการรันซ้ำ 12 Paths; ออร์เดอร์แต่ละ Path ยังมี Count ในรายงาน

## ยังไม่ทำในรุ่นนี้
- การเชื่อมต่อ Live WebSocket, Account Stream, Nonce/Client Order IDs ของ Exchange จริง
- การส่ง/ยกเลิก/เช็กออร์เดอร์เงินจริง หรือ Recovery เมื่ออินเทอร์เน็ตหลุด
- ตรวจบังคับใช้ Rate Limit จริงตาม Tier ของผู้ใช้, Margin/Stop Loss/Kill Switch จริง
- Simulate Exit partial/failure, order fee จริงตามบัญชี

ต้องทำ Live Execution State Machine แบบ Separate Module และ Paper-forward ก่อนพิจารณาส่งเงินจริง
