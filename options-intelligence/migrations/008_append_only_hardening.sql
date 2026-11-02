-- Hardening (found in review): row-level triggers don't fire for TRUNCATE.
-- Add statement-level guards so TRUNCATE/UPDATE/DELETE on audit tables always fails
-- for ordinary roles. (A superuser can still disable triggers; the hash chains and
-- reconciliation then detect tampering.)

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['paper_ledger_events', 'approval_records', 'ai_agent_versions', 'ai_evidence_packs',
                             'ai_meeting_turns', 'ai_decisions', 'ai_disagreements', 'ai_predictions', 'ai_prediction_outcomes']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_no_truncate', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION doi_forbid_mutation()',
                       t || '_no_truncate', t);
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_stmt_guard', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH STATEMENT EXECUTE FUNCTION doi_forbid_mutation()',
                       t || '_stmt_guard', t);
    END LOOP;
END
$$;
