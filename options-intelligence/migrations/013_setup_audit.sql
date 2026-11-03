-- Additive: Setup Wizard changes and restarts in the hash-chained approval log.
ALTER TABLE approval_records DROP CONSTRAINT IF EXISTS approval_records_action_check;
ALTER TABLE approval_records ADD CONSTRAINT approval_records_action_check CHECK (action IN (
    'APPROVE', 'REJECT', 'PROMOTE', 'ROLLBACK', 'KILL_SWITCH_ON', 'KILL_SWITCH_OFF', 'BUDGET_CHANGE', 'PAPER_ORDER',
    'PAPER_CLOSE', 'TEST_APPROVAL', 'SECRET_SET', 'SECRET_DELETE', 'API_KEY_CREATE', 'API_KEY_REVOKE',
    'SETUP_CHANGE', 'RESTART'));
