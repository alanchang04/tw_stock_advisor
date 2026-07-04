-- Migration 09 — 細分族群 + 龍頭股（族群輪動儀表板）
-- 執行：Neon SQL Editor 貼上執行一次
-- 官方產業分類太粗（半導體業一包 210 支），此表提供記憶體/AI伺服器等細分族群

CREATE TABLE IF NOT EXISTS stock_groups (
    group_code  VARCHAR(30) PRIMARY KEY,
    group_name  VARCHAR(50) NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS stock_group_members (
    group_code  VARCHAR(30) REFERENCES stock_groups(group_code),
    stock_id    VARCHAR(10) REFERENCES stocks(stock_id),
    is_leader   BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (group_code, stock_id)
);

INSERT INTO stock_groups (group_code, group_name) VALUES
    ('memory',        '記憶體'),
    ('foundry',       '晶圓代工'),
    ('ic_design',     'IC設計'),
    ('packaging',     '封測'),
    ('pcb',           'PCB/載板'),
    ('ai_server',     'AI伺服器'),
    ('cooling',       '散熱'),
    ('passive',       '被動元件'),
    ('optical',       '光學'),
    ('networking',    '網通'),
    ('financial',     '金控'),
    ('shipping',      '航運'),
    ('defense_drone', '軍工/無人機')
ON CONFLICT (group_code) DO NOTHING;

-- 成員 + 龍頭標記（JOIN stocks 確保代號存在，不存在自動跳過）
INSERT INTO stock_group_members (group_code, stock_id, is_leader)
SELECT v.g, v.sid, v.lead
FROM (VALUES
    ('memory','2344',TRUE),  ('memory','2408',TRUE),  ('memory','3006',FALSE),
    ('memory','2337',FALSE), ('memory','8299',FALSE),
    ('foundry','2330',TRUE), ('foundry','2303',FALSE),('foundry','6770',FALSE),('foundry','5347',FALSE),
    ('ic_design','2454',TRUE),('ic_design','3034',FALSE),('ic_design','2379',FALSE),
    ('ic_design','3443',FALSE),('ic_design','3529',FALSE),
    ('packaging','3711',TRUE),('packaging','6239',FALSE),('packaging','2449',FALSE),
    ('pcb','3037',TRUE),     ('pcb','8046',FALSE),    ('pcb','2383',FALSE),('pcb','6213',FALSE),
    ('ai_server','2382',TRUE),('ai_server','2317',FALSE),('ai_server','3231',FALSE),
    ('ai_server','6669',FALSE),('ai_server','2376',FALSE),
    ('cooling','3017',TRUE), ('cooling','2421',FALSE),('cooling','3324',FALSE),
    ('passive','2327',TRUE), ('passive','2492',FALSE),('passive','2375',FALSE),
    ('optical','3008',TRUE), ('optical','3406',FALSE),
    ('networking','2345',TRUE),('networking','5388',FALSE),('networking','3596',FALSE),
    ('financial','2886',TRUE),('financial','2891',FALSE),('financial','2884',FALSE),('financial','2882',FALSE),
    ('shipping','2603',TRUE),('shipping','2609',FALSE),('shipping','2615',FALSE),
    ('defense_drone','2634',TRUE),('defense_drone','8033',FALSE),('defense_drone','3162',FALSE)
) AS v(g, sid, lead)
JOIN stocks s ON s.stock_id = v.sid
ON CONFLICT (group_code, stock_id) DO NOTHING;
