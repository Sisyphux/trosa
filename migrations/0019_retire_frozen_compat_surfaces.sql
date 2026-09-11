-- Retire runtime compatibility surfaces for frozen AI research, customer
-- understanding/recommendation and website-monitoring features.
--
-- The canonical historical tables remain intact for audit and migration
-- tooling. Only the old SQLite-shaped views and their write triggers are
-- removed, so current PostgreSQL runtime code cannot accidentally reopen
-- those features through the compatibility schema.
BEGIN;

DROP VIEW IF EXISTS trade_os_compat.research_reports CASCADE;
DROP VIEW IF EXISTS trade_os_compat.external_analysis_notes CASCADE;
DROP VIEW IF EXISTS trade_os_compat.customer_understandings CASCADE;
DROP VIEW IF EXISTS trade_os_compat.ai_recommendations CASCADE;
DROP VIEW IF EXISTS trade_os_compat.web_monitor_logs CASCADE;

DROP FUNCTION IF EXISTS trade_os_compat.research_reports_write() CASCADE;
DROP FUNCTION IF EXISTS trade_os_compat.external_analysis_notes_write() CASCADE;
DROP FUNCTION IF EXISTS trade_os_compat.customer_understandings_write() CASCADE;
DROP FUNCTION IF EXISTS trade_os_compat.ai_recommendations_write() CASCADE;
DROP FUNCTION IF EXISTS trade_os_compat.web_monitor_logs_write() CASCADE;

COMMIT;
