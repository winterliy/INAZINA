-- work.inazina.localhost schema (MySQL 8.4, database inazina_work). Separate from the main PostgreSQL on purpose.
-- user_ref = the account id from the main INAZINA login (id.); there is no cross-database foreign key, the work API checks it per request.
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS schema_version (version INT PRIMARY KEY, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP);

-- 계정/조직
CREATE TABLE IF NOT EXISTS members (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, user_ref VARCHAR(64) NOT NULL UNIQUE, display_name VARCHAR(80) NOT NULL,
  job_title VARCHAR(80) NULL, phone VARCHAR(40) NULL, status ENUM('active','leave','retired') NOT NULL DEFAULT 'active',
  role ENUM('admin','member','guest') NOT NULL DEFAULT 'member', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS departments (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, parent_id BIGINT NULL, name VARCHAR(80) NOT NULL, sort_order INT NOT NULL DEFAULT 0,
  FOREIGN KEY (parent_id) REFERENCES departments(id) ON DELETE SET NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS department_members (
  department_id BIGINT NOT NULL, member_id BIGINT NOT NULL, is_head TINYINT(1) NOT NULL DEFAULT 0, PRIMARY KEY (department_id, member_id),
  FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE, FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 연락처
CREATE TABLE IF NOT EXISTS contacts (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, owner_id BIGINT NULL, name VARCHAR(80) NOT NULL, organization VARCHAR(120) NULL,
  email VARCHAR(190) NULL, phone VARCHAR(40) NULL, memo TEXT NULL, shared TINYINT(1) NOT NULL DEFAULT 0,
  FOREIGN KEY (owner_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 내부 메일(쪽지)
CREATE TABLE IF NOT EXISTS mail_messages (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, sender_id BIGINT NOT NULL, subject VARCHAR(200) NOT NULL, body MEDIUMTEXT NOT NULL,
  sent_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (sender_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS mail_recipients (
  message_id BIGINT NOT NULL, member_id BIGINT NOT NULL, kind ENUM('to','cc') NOT NULL DEFAULT 'to', read_at DATETIME NULL,
  folder ENUM('inbox','trash') NOT NULL DEFAULT 'inbox', PRIMARY KEY (message_id, member_id),
  FOREIGN KEY (message_id) REFERENCES mail_messages(id) ON DELETE CASCADE, FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 메신저
CREATE TABLE IF NOT EXISTS chat_channels (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, kind ENUM('direct','group') NOT NULL, name VARCHAR(80) NULL, created_by BIGINT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (created_by) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS chat_members (
  channel_id BIGINT NOT NULL, member_id BIGINT NOT NULL, last_read_message_id BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (channel_id, member_id),
  FOREIGN KEY (channel_id) REFERENCES chat_channels(id) ON DELETE CASCADE, FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS chat_messages (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, channel_id BIGINT NOT NULL, sender_id BIGINT NOT NULL, parent_id BIGINT NULL, body TEXT NOT NULL,
  file_id BIGINT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at DATETIME NULL, KEY (channel_id, id),
  FOREIGN KEY (channel_id) REFERENCES chat_channels(id) ON DELETE CASCADE, FOREIGN KEY (sender_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 캘린더
CREATE TABLE IF NOT EXISTS calendars (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, owner_id BIGINT NULL, name VARCHAR(80) NOT NULL, kind ENUM('personal','shared','resource') NOT NULL,
  FOREIGN KEY (owner_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS calendar_events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, calendar_id BIGINT NOT NULL, title VARCHAR(200) NOT NULL, starts_at DATETIME NOT NULL, ends_at DATETIME NOT NULL,
  all_day TINYINT(1) NOT NULL DEFAULT 0, location VARCHAR(160) NULL, memo TEXT NULL, created_by BIGINT NOT NULL, KEY (calendar_id, starts_at),
  FOREIGN KEY (calendar_id) REFERENCES calendars(id) ON DELETE CASCADE, FOREIGN KEY (created_by) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS calendar_attendees (
  event_id BIGINT NOT NULL, member_id BIGINT NOT NULL, response ENUM('pending','yes','no','maybe') NOT NULL DEFAULT 'pending', PRIMARY KEY (event_id, member_id),
  FOREIGN KEY (event_id) REFERENCES calendar_events(id) ON DELETE CASCADE, FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 드라이브
CREATE TABLE IF NOT EXISTS drive_nodes (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, parent_id BIGINT NULL, owner_id BIGINT NOT NULL, kind ENUM('folder','file') NOT NULL, name VARCHAR(190) NOT NULL,
  size_bytes BIGINT NOT NULL DEFAULT 0, mime VARCHAR(120) NULL, storage_key VARCHAR(190) NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  trashed_at DATETIME NULL, KEY (parent_id), FOREIGN KEY (parent_id) REFERENCES drive_nodes(id) ON DELETE CASCADE, FOREIGN KEY (owner_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS drive_shares (
  node_id BIGINT NOT NULL, member_id BIGINT NULL, department_id BIGINT NULL, permission ENUM('view','edit') NOT NULL DEFAULT 'view',
  FOREIGN KEY (node_id) REFERENCES drive_nodes(id) ON DELETE CASCADE, FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE,
  FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 문서 협업
CREATE TABLE IF NOT EXISTS docs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, title VARCHAR(200) NOT NULL, owner_id BIGINT NOT NULL, content MEDIUMTEXT NOT NULL, version INT NOT NULL DEFAULT 1,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, FOREIGN KEY (owner_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS doc_versions (
  doc_id BIGINT NOT NULL, version INT NOT NULL, content MEDIUMTEXT NOT NULL, edited_by BIGINT NOT NULL, edited_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (doc_id, version), FOREIGN KEY (doc_id) REFERENCES docs(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS doc_members (
  doc_id BIGINT NOT NULL, member_id BIGINT NOT NULL, permission ENUM('view','comment','edit') NOT NULL DEFAULT 'view', PRIMARY KEY (doc_id, member_id),
  FOREIGN KEY (doc_id) REFERENCES docs(id) ON DELETE CASCADE, FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS doc_comments (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, doc_id BIGINT NOT NULL, author_id BIGINT NOT NULL, body TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (doc_id) REFERENCES docs(id) ON DELETE CASCADE, FOREIGN KEY (author_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 전자결재
CREATE TABLE IF NOT EXISTS approval_forms (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(80) NOT NULL, template MEDIUMTEXT NOT NULL, active TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS approvals (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, form_id BIGINT NOT NULL, drafter_id BIGINT NOT NULL, title VARCHAR(200) NOT NULL, body MEDIUMTEXT NOT NULL,
  status ENUM('draft','pending','approved','rejected','withdrawn') NOT NULL DEFAULT 'draft', submitted_at DATETIME NULL, closed_at DATETIME NULL,
  FOREIGN KEY (form_id) REFERENCES approval_forms(id), FOREIGN KEY (drafter_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS approval_steps (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, approval_id BIGINT NOT NULL, seq INT NOT NULL, approver_id BIGINT NOT NULL, kind ENUM('approve','agree','cc') NOT NULL DEFAULT 'approve',
  decision ENUM('waiting','approved','rejected') NOT NULL DEFAULT 'waiting', comment TEXT NULL, decided_at DATETIME NULL, KEY (approver_id, decision),
  FOREIGN KEY (approval_id) REFERENCES approvals(id) ON DELETE CASCADE, FOREIGN KEY (approver_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 게시판/공지
CREATE TABLE IF NOT EXISTS boards (id BIGINT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(80) NOT NULL, notice_only TINYINT(1) NOT NULL DEFAULT 0) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS board_posts (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, board_id BIGINT NOT NULL, author_id BIGINT NOT NULL, title VARCHAR(200) NOT NULL, body MEDIUMTEXT NOT NULL,
  pinned TINYINT(1) NOT NULL DEFAULT 0, must_read TINYINT(1) NOT NULL DEFAULT 0, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE, FOREIGN KEY (author_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS board_comments (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, post_id BIGINT NOT NULL, author_id BIGINT NOT NULL, body TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (post_id) REFERENCES board_posts(id) ON DELETE CASCADE, FOREIGN KEY (author_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS board_reads (post_id BIGINT NOT NULL, member_id BIGINT NOT NULL, read_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (post_id, member_id),
  FOREIGN KEY (post_id) REFERENCES board_posts(id) ON DELETE CASCADE, FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE) ENGINE=InnoDB;

-- 프로젝트/업무
CREATE TABLE IF NOT EXISTS projects (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(120) NOT NULL, owner_id BIGINT NOT NULL, status ENUM('active','done','archived') NOT NULL DEFAULT 'active',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (owner_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS project_members (project_id BIGINT NOT NULL, member_id BIGINT NOT NULL, PRIMARY KEY (project_id, member_id),
  FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE, FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS tasks (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, project_id BIGINT NOT NULL, title VARCHAR(200) NOT NULL, description TEXT NULL, assignee_id BIGINT NULL,
  status ENUM('todo','doing','done') NOT NULL DEFAULT 'todo', due_date DATE NULL, label VARCHAR(40) NULL, sort_order INT NOT NULL DEFAULT 0, KEY (assignee_id, status),
  FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE, FOREIGN KEY (assignee_id) REFERENCES members(id) ON DELETE SET NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS task_comments (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, task_id BIGINT NOT NULL, author_id BIGINT NOT NULL, body TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE, FOREIGN KEY (author_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 근태/HR
CREATE TABLE IF NOT EXISTS attendance (
  member_id BIGINT NOT NULL, work_date DATE NOT NULL, clock_in DATETIME NULL, clock_out DATETIME NULL, PRIMARY KEY (member_id, work_date),
  FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS leave_requests (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, member_id BIGINT NOT NULL, kind ENUM('annual','half','sick','other') NOT NULL, starts_on DATE NOT NULL, ends_on DATE NOT NULL,
  reason VARCHAR(300) NULL, approval_id BIGINT NULL, status ENUM('pending','approved','rejected','cancelled') NOT NULL DEFAULT 'pending',
  FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE, FOREIGN KEY (approval_id) REFERENCES approvals(id) ON DELETE SET NULL
) ENGINE=InnoDB;

-- 알림 + 감사 로그(전 기능 공통)
CREATE TABLE IF NOT EXISTS notifications (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, member_id BIGINT NOT NULL, kind VARCHAR(40) NOT NULL, ref_id BIGINT NULL, text VARCHAR(300) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, read_at DATETIME NULL, KEY (member_id, read_at), FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS audit_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, member_id BIGINT NULL, action VARCHAR(60) NOT NULL, target VARCHAR(120) NULL, at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, KEY (at)
) ENGINE=InnoDB;

INSERT IGNORE INTO schema_version (version) VALUES (1);
