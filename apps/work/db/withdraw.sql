-- Withdrawal of a main-account holder (policy decided 2026-11-28: anonymise, keep organisational records).
-- Deleted: personal data (received mail, undelivered sent mail, chat, drive, docs the member owns, contacts, calendars, notifications, tasks' assignment, drafts).
-- Kept, shown as "탈퇴한 사용자": approvals, attendance, leave, board posts/comments, project tasks, audit log. The member row itself is
-- anonymised, so everything that points at it is anonymised with it. Callable many times; unknown user_ref does nothing.
CREATE PROCEDURE work_withdraw(IN p_user_ref VARCHAR(64))
BEGIN
  DECLARE v_id BIGINT;
    SELECT id INTO v_id FROM members WHERE user_ref = p_user_ref LIMIT 1;
  IF v_id IS NOT NULL THEN
    START TRANSACTION;
    DELETE FROM mail_recipients WHERE member_id = v_id;
    -- mail already delivered stays in the recipients' boxes (like real mail); a sent message nobody holds any more is removed
    DELETE m FROM mail_messages m WHERE m.sender_id = v_id AND NOT EXISTS (SELECT 1 FROM mail_recipients r WHERE r.message_id = m.id);
    DELETE FROM chat_messages WHERE sender_id = v_id;
    DELETE FROM chat_members WHERE member_id = v_id;
    DELETE FROM chat_channels WHERE kind = 'direct' AND created_by = v_id;
    DELETE FROM drive_nodes WHERE owner_id = v_id;
    DELETE FROM docs WHERE owner_id = v_id;
    DELETE FROM doc_members WHERE member_id = v_id;
    DELETE FROM doc_comments WHERE author_id = v_id;
    DELETE FROM contacts WHERE owner_id = v_id;
    DELETE FROM calendars WHERE owner_id = v_id;
    DELETE FROM calendar_attendees WHERE member_id = v_id;
    DELETE FROM notifications WHERE member_id = v_id;
    DELETE FROM department_members WHERE member_id = v_id;
    DELETE FROM project_members WHERE member_id = v_id;
    DELETE FROM approvals WHERE drafter_id = v_id AND status = 'draft';
    UPDATE tasks SET assignee_id = NULL WHERE assignee_id = v_id AND status <> 'done';
    DELETE FROM sessions WHERE member_id = v_id;
    UPDATE members SET login_id = NULL, password_hash = NULL, failed_logins = 0, locked_until = NULL, user_ref = CONCAT('w-', SUBSTRING(MD5(CONCAT(p_user_ref, RAND(), NOW(6))), 1, 16)), display_name = '탈퇴한 사용자',
      job_title = NULL, phone = NULL, status = 'retired', role = 'guest' WHERE id = v_id;
    INSERT INTO audit_log (member_id, action, target) VALUES (v_id, 'withdraw', 'anonymised');
    COMMIT;
  END IF;
END
