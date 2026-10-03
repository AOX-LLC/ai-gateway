--
-- PostgreSQL database dump
--

\restrict VC0KKUfeY5Zdo2yCngpdbtwgZokCSYcuZrJLoptKUAEJA8WxKWHSqT2JyQAdFmP

-- Dumped from database version 17.11 (Debian 17.11-1.pgdg12+2)
-- Dumped by pg_dump version 17.11 (Debian 17.11-1.pgdg12+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: policy; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA policy;


--
-- Name: agent_core_audit_append_at_end(); Type: FUNCTION; Schema: policy; Owner: -
--

CREATE FUNCTION policy.agent_core_audit_append_at_end() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
    DECLARE last_seq bigint;
    BEGIN
        EXECUTE format('SELECT COALESCE(MAX(seq), 0) FROM %I.%I', TG_TABLE_SCHEMA, TG_TABLE_NAME)
            INTO last_seq;
        IF NEW.seq <> last_seq + 1 THEN
            RAISE EXCEPTION 'agent_core_audit is append-only';
        END IF;
        RETURN NEW;
    END $$;


--
-- Name: agent_core_audit_refuse_change(); Type: FUNCTION; Schema: policy; Owner: -
--

CREATE FUNCTION policy.agent_core_audit_refuse_change() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
    BEGIN RAISE EXCEPTION 'agent_core_audit is append-only'; END $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_core_approvals; Type: TABLE; Schema: policy; Owner: -
--

CREATE TABLE policy.agent_core_approvals (
    id text NOT NULL,
    action text NOT NULL,
    summary text NOT NULL,
    payload_sha256 text NOT NULL,
    requested_by text NOT NULL,
    required_role text NOT NULL,
    created_at text NOT NULL,
    expires_at text NOT NULL,
    status text NOT NULL,
    decision text,
    resolved_by text,
    resolved_at text,
    consumed_at text,
    reason text,
    run_context text
);


--
-- Name: agent_core_audit; Type: TABLE; Schema: policy; Owner: -
--

CREATE TABLE policy.agent_core_audit (
    seq bigint NOT NULL,
    schema_version integer NOT NULL,
    event_id text NOT NULL,
    occurred_at text NOT NULL,
    action text NOT NULL,
    actor_id text NOT NULL,
    subject_id text,
    payload text NOT NULL,
    run_context text,
    prev_hash text NOT NULL,
    record_hash text NOT NULL
);


--
-- Data for Name: agent_core_approvals; Type: TABLE DATA; Schema: policy; Owner: -
--

INSERT INTO policy.agent_core_approvals VALUES ('7eff70a1-3f92-48c9-a957-1f60b4ec6a55', 'tickets__change_status', 'a pending request (fixture)', '5cbbfb997c00ba766b67e7e9b4d78255e10122a174000cda8203d09199191e3f', 'client:00000000-0000-4000-8000-000000000001', 'approver', '2026-10-03T04:55:36.266742Z', '2026-10-10T04:55:36.266742Z', 'pending', NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO policy.agent_core_approvals VALUES ('79226c46-fe0d-411f-bf67-dceaac8fb705', 'tickets__change_status', 'a approved request (fixture)', '423cc138b77ee91787a7d9004c1fed772f1cf54106a8e74401873bbd298cfc44', 'client:00000000-0000-4000-8000-000000000001', 'approver', '2026-10-03T04:55:36.288957Z', '2026-10-10T04:55:36.288957Z', 'approved', 'approve', 'human:fixture', '2026-10-03T04:55:36.322542Z', NULL, NULL, NULL);
INSERT INTO policy.agent_core_approvals VALUES ('240c4b8d-0fcb-479b-a3dc-05e98ab98887', 'tickets__change_status', 'a rejected request (fixture)', 'c8bd17eb6cefc342afd7ab31846da0580fe671611ed1808b899c686786956ad3', 'client:00000000-0000-4000-8000-000000000001', 'approver', '2026-10-03T04:55:36.333437Z', '2026-10-10T04:55:36.333437Z', 'rejected', 'reject', 'human:fixture', '2026-10-03T04:55:36.377874Z', NULL, NULL, NULL);
INSERT INTO policy.agent_core_approvals VALUES ('a7631a31-40a0-4798-9213-d99fd8c3a9e4', 'tickets__change_status', 'a consumed request (fixture)', '8d2c1018efd91bcd142aa513f6a02c521178b37f6160a6de06e06ac823b8cf98', 'client:00000000-0000-4000-8000-000000000001', 'approver', '2026-10-03T04:55:36.388824Z', '2026-10-10T04:55:36.388824Z', 'consumed', 'approve', 'human:fixture', '2026-10-03T04:55:36.422270Z', '2026-10-03T04:55:36.444849Z', NULL, NULL);


--
-- Data for Name: agent_core_audit; Type: TABLE DATA; Schema: policy; Owner: -
--

INSERT INTO policy.agent_core_audit VALUES (1, 2, '1b43cb78-8458-47e4-ad12-cf692bbfaad4', '2026-10-03T04:55:36.117683Z', 'gateway.tool_call', 'client:00000000-0000-4000-8000-000000000001', 'tickets__get_ticket', '{"n":1,"outcome":"forwarded","request_id":"722b5db0-e564-4991-a3f9-238cbbd75b78"}', NULL, '0000000000000000000000000000000000000000000000000000000000000000', '5f0f1abc888ef9773cd41096a3c6a8a536570e36e1a3667c8b0240342d0126e8');
INSERT INTO policy.agent_core_audit VALUES (2, 2, 'b72fc06e-0fa5-4f21-9760-eccd5601e26a', '2026-10-03T04:55:36.145198Z', 'gateway.tool_call', 'client:00000000-0000-4000-8000-000000000001', 'tickets__get_ticket', '{"n":2,"outcome":"forwarded","request_id":"69b87b8a-09db-4e2e-aa8f-13f0767ab216"}', NULL, '5f0f1abc888ef9773cd41096a3c6a8a536570e36e1a3667c8b0240342d0126e8', '254b038f9c8b896bc48e77b031c33ed2e65bec5227abd44131e1ba7808d72f08');
INSERT INTO policy.agent_core_audit VALUES (3, 2, '4e933e06-974c-492b-a0d1-fa77565d2558', '2026-10-03T04:55:36.182898Z', 'gateway.tool_call', 'client:00000000-0000-4000-8000-000000000001', 'tickets__get_ticket', '{"n":3,"outcome":"forwarded","request_id":"317d5c4e-70e1-4c2c-beee-a73233062310"}', NULL, '254b038f9c8b896bc48e77b031c33ed2e65bec5227abd44131e1ba7808d72f08', '25583bb0f18e2f250631aa90fbd205ff6be20ab8ce0812207a00b342b1e7f2f4');
INSERT INTO policy.agent_core_audit VALUES (4, 2, '8d37d92f-b7fd-45d3-b5c7-214b96a8ffc3', '2026-10-03T04:55:36.233854Z', 'gateway.tool_call', 'client:00000000-0000-4000-8000-000000000001', 'tickets__get_ticket', '{"n":4,"outcome":"forwarded","request_id":"ee924638-b8ba-4628-8494-302ea58b33cc"}', NULL, '25583bb0f18e2f250631aa90fbd205ff6be20ab8ce0812207a00b342b1e7f2f4', 'd10148774a49212473daf45f83b637fd4c0a889f8979e03fd5331eb7a31b46bd');
INSERT INTO policy.agent_core_audit VALUES (5, 2, '9389a5a7-95b7-4ad7-ab9d-6deae6dc0846', '2026-10-03T04:55:36.255215Z', 'gateway.tool_call', 'client:00000000-0000-4000-8000-000000000001', 'tickets__get_ticket', '{"n":5,"outcome":"forwarded","request_id":"a478a32c-397e-4aca-b3c6-5ce07bb56014"}', NULL, 'd10148774a49212473daf45f83b637fd4c0a889f8979e03fd5331eb7a31b46bd', '7a69835a9cd068d08b7f6e300ba9998c71e9e461b2e8d10d027e5f662faf9029');
INSERT INTO policy.agent_core_audit VALUES (6, 2, 'db29a891-2a8b-4309-b053-1d8c723fd92b', '2026-10-03T04:55:36.281209Z', 'approval.requested', 'client:00000000-0000-4000-8000-000000000001', '7eff70a1-3f92-48c9-a957-1f60b4ec6a55', '{"approval_action":"tickets__change_status","payload_sha256":"5cbbfb997c00ba766b67e7e9b4d78255e10122a174000cda8203d09199191e3f","required_role":"approver"}', NULL, '7a69835a9cd068d08b7f6e300ba9998c71e9e461b2e8d10d027e5f662faf9029', '553a559c891a4ab9ce4d265a1d892ba4aa1802fed53c397a44b2ab7fadb53d1a');
INSERT INTO policy.agent_core_audit VALUES (7, 2, 'ab421436-0a3e-4772-bc24-3e40b974cebe', '2026-10-03T04:55:36.301697Z', 'approval.requested', 'client:00000000-0000-4000-8000-000000000001', '79226c46-fe0d-411f-bf67-dceaac8fb705', '{"approval_action":"tickets__change_status","payload_sha256":"423cc138b77ee91787a7d9004c1fed772f1cf54106a8e74401873bbd298cfc44","required_role":"approver"}', NULL, '553a559c891a4ab9ce4d265a1d892ba4aa1802fed53c397a44b2ab7fadb53d1a', '7e28f5af0c6e19255667a66b0e1fd5c85de14f58284e64ea40284ed6dd667aae');
INSERT INTO policy.agent_core_audit VALUES (8, 2, '9639eab1-af12-4480-a3b9-2af4e0ce9781', '2026-10-03T04:55:36.325902Z', 'approval.resolved', 'human:fixture', '79226c46-fe0d-411f-bf67-dceaac8fb705', '{"approval_action":"tickets__change_status","decision":"approve"}', NULL, '7e28f5af0c6e19255667a66b0e1fd5c85de14f58284e64ea40284ed6dd667aae', '88330fc0d1176ca4336906fc6a879b19677e89838c3416f3755b198791364d2b');
INSERT INTO policy.agent_core_audit VALUES (9, 2, 'bca7a86b-8459-4f58-93e2-00b17353c11b', '2026-10-03T04:55:36.345669Z', 'approval.requested', 'client:00000000-0000-4000-8000-000000000001', '240c4b8d-0fcb-479b-a3dc-05e98ab98887', '{"approval_action":"tickets__change_status","payload_sha256":"c8bd17eb6cefc342afd7ab31846da0580fe671611ed1808b899c686786956ad3","required_role":"approver"}', NULL, '88330fc0d1176ca4336906fc6a879b19677e89838c3416f3755b198791364d2b', '1d10311809c03a7bd909fbd0934a458d247b2dd2634aa6aeec98d928c8a613d5');
INSERT INTO policy.agent_core_audit VALUES (10, 2, '0b8f8014-9641-426d-86d0-8e269eeb8ccf', '2026-10-03T04:55:36.379278Z', 'approval.resolved', 'human:fixture', '240c4b8d-0fcb-479b-a3dc-05e98ab98887', '{"approval_action":"tickets__change_status","decision":"reject"}', NULL, '1d10311809c03a7bd909fbd0934a458d247b2dd2634aa6aeec98d928c8a613d5', '37bac0aff522b264a8c51d3cbc191cc0160877d12c640799b83500eb7058d8c4');
INSERT INTO policy.agent_core_audit VALUES (11, 2, '4d939826-3a82-498f-94ca-9ba924ed6dab', '2026-10-03T04:55:36.400738Z', 'approval.requested', 'client:00000000-0000-4000-8000-000000000001', 'a7631a31-40a0-4798-9213-d99fd8c3a9e4', '{"approval_action":"tickets__change_status","payload_sha256":"8d2c1018efd91bcd142aa513f6a02c521178b37f6160a6de06e06ac823b8cf98","required_role":"approver"}', NULL, '37bac0aff522b264a8c51d3cbc191cc0160877d12c640799b83500eb7058d8c4', '6546f08a014f67b89ba23f5d827dda6f6279553ab0687a74d73f28bbef58bc65');
INSERT INTO policy.agent_core_audit VALUES (12, 2, 'a2b56b6e-50f2-4a69-872b-47ddac07b94d', '2026-10-03T04:55:36.423797Z', 'approval.resolved', 'human:fixture', 'a7631a31-40a0-4798-9213-d99fd8c3a9e4', '{"approval_action":"tickets__change_status","decision":"approve"}', NULL, '6546f08a014f67b89ba23f5d827dda6f6279553ab0687a74d73f28bbef58bc65', '6996b4b2da796c158faf1c0d5e81907a1549318b1c50462a11cd5b940b183bfb');
INSERT INTO policy.agent_core_audit VALUES (13, 2, 'fe2c62a5-2248-43d9-b4f1-ca7c7172cf07', '2026-10-03T04:55:36.446399Z', 'approval.consumed', 'client:00000000-0000-4000-8000-000000000001', 'a7631a31-40a0-4798-9213-d99fd8c3a9e4', '{"approval_action":"tickets__change_status"}', NULL, '6996b4b2da796c158faf1c0d5e81907a1549318b1c50462a11cd5b940b183bfb', '89d7405782f3aa09eac5cef98ced714f729b365d8c48238798eaf8cee089d3ca');


--
-- Name: agent_core_approvals agent_core_approvals_pkey; Type: CONSTRAINT; Schema: policy; Owner: -
--

ALTER TABLE ONLY policy.agent_core_approvals
    ADD CONSTRAINT agent_core_approvals_pkey PRIMARY KEY (id);


--
-- Name: agent_core_audit agent_core_audit_event_id_key; Type: CONSTRAINT; Schema: policy; Owner: -
--

ALTER TABLE ONLY policy.agent_core_audit
    ADD CONSTRAINT agent_core_audit_event_id_key UNIQUE (event_id);


--
-- Name: agent_core_audit agent_core_audit_pkey; Type: CONSTRAINT; Schema: policy; Owner: -
--

ALTER TABLE ONLY policy.agent_core_audit
    ADD CONSTRAINT agent_core_audit_pkey PRIMARY KEY (seq);


--
-- Name: agent_core_approvals_pending; Type: INDEX; Schema: policy; Owner: -
--

CREATE INDEX agent_core_approvals_pending ON policy.agent_core_approvals USING btree (status, created_at, id);


--
-- Name: agent_core_audit agent_core_audit_append_at_end; Type: TRIGGER; Schema: policy; Owner: -
--

CREATE TRIGGER agent_core_audit_append_at_end BEFORE INSERT ON policy.agent_core_audit FOR EACH ROW EXECUTE FUNCTION policy.agent_core_audit_append_at_end();


--
-- Name: agent_core_audit agent_core_audit_no_truncate; Type: TRIGGER; Schema: policy; Owner: -
--

CREATE TRIGGER agent_core_audit_no_truncate BEFORE TRUNCATE ON policy.agent_core_audit FOR EACH STATEMENT EXECUTE FUNCTION policy.agent_core_audit_refuse_change();


--
-- Name: agent_core_audit agent_core_audit_no_update_delete; Type: TRIGGER; Schema: policy; Owner: -
--

CREATE TRIGGER agent_core_audit_no_update_delete BEFORE DELETE OR UPDATE ON policy.agent_core_audit FOR EACH ROW EXECUTE FUNCTION policy.agent_core_audit_refuse_change();


--
-- PostgreSQL database dump complete
--

\unrestrict VC0KKUfeY5Zdo2yCngpdbtwgZokCSYcuZrJLoptKUAEJA8WxKWHSqT2JyQAdFmP

