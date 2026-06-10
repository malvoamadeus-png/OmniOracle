-- 更新 address_metrics 表中的 source_tags，将 LEAGUE OF LEGENDS 替换为 LOL
-- 运行此脚本前请先备份数据

-- 更新包含 "LEAGUE OF LEGENDS" 的 source_tags
UPDATE address_metrics
SET source_tags = REPLACE(source_tags, 'LEAGUE OF LEGENDS', 'LOL')
WHERE source_tags LIKE '%LEAGUE OF LEGENDS%';

-- 验证更新结果
SELECT COUNT(*) as updated_count
FROM address_metrics
WHERE source_tags LIKE '%LOL%';
