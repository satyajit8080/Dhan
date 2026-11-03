-- Additive: allow settings/key-management actions in the hash-chained approval log (values are never logged).
DO $$
DECLARE c text;
BEGIN
    SELECT conname INTO c FROM pg_constraint
     WHERE conrelid = 'approval_records'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%KILL_SWITCH_ON%';
    IF c IS NOT NULL THEN
        EXECUTE format('ALTER TABLE approval_records DROP CONSTRAINT %I', c);
    END IF;
END $$;
ALTER TABLE approval_records ADD CONSTRAINT approval_records_action_check CHECK (action IN (
    'APPROVE', 'REJECT', 'PROMOTE', 'ROLLBACK', 'KILL_SWITCH_ON', 'KILL_SWITCH_OFF', 'BUDGET_CHANGE', 'PAPER_ORDER',
    'PAPER_CLOSE', 'TEST_APPROVAL', 'SECRET_SET', 'SECRET_DELETE', 'API_KEY_CREATE', 'API_KEY_REVOKE'));
