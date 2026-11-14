CREATE TABLE migration_runs (
	id VARCHAR(64) NOT NULL,
	state VARCHAR(20) NOT NULL,
	manifest JSONB DEFAULT '{}'::jsonb NOT NULL,
	report JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
)
-- statement boundary
CREATE TABLE onboarding_candidates (
	telegram_chat_id BIGSERIAL NOT NULL,
	inviter_id BIGINT,
	title TEXT,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (telegram_chat_id)
)
-- statement boundary
CREATE TABLE onboarding_requests (
	id UUID NOT NULL,
	owner_id BIGINT NOT NULL,
	manager_id BIGINT NOT NULL,
	work_actor_id BIGINT NOT NULL,
	admin_actor_id BIGINT NOT NULL,
	work_chat_id BIGINT,
	admin_chat_id BIGINT,
	work_confirmed BOOLEAN DEFAULT 'false' NOT NULL,
	admin_confirmed BOOLEAN DEFAULT 'false' NOT NULL,
	state VARCHAR(20) DEFAULT 'pending' NOT NULL,
	version INTEGER DEFAULT '1' NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
)
-- statement boundary
CREATE TABLE polling_cursors (
	bot_id BIGSERIAL NOT NULL,
	next_offset BIGINT NOT NULL,
	PRIMARY KEY (bot_id)
)
-- statement boundary
CREATE TABLE admin_bindings (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	admin_chat_id BIGINT NOT NULL,
	version INTEGER NOT NULL,
	confirmed_by BIGINT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	closed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE UNIQUE INDEX uq_active_binding ON admin_bindings (chat_id) WHERE closed_at IS NULL
-- statement boundary
CREATE TABLE audit_events (
	id UUID NOT NULL,
	chat_id INTEGER,
	actor_id BIGINT,
	kind VARCHAR(64) NOT NULL,
	details JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE INDEX ix_audit_events_chat_id_created_at_id ON audit_events (chat_id, created_at, id)
-- statement boundary
CREATE TABLE budget_reservations (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	day VARCHAR(10) NOT NULL,
	key VARCHAR(220) NOT NULL,
	reserved FLOAT NOT NULL,
	spent FLOAT,
	state VARCHAR(16) DEFAULT 'reserved' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	UNIQUE (key)
)
-- statement boundary
CREATE TABLE deny_entries (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	reason TEXT NOT NULL,
	actor_id BIGINT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)
-- statement boundary
CREATE UNIQUE INDEX uq_active_deny ON deny_entries (chat_id, user_id) WHERE revoked_at IS NULL
-- statement boundary
CREATE TABLE group_id_aliases (
	telegram_chat_id BIGSERIAL NOT NULL,
	chat_id INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (telegram_chat_id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE TABLE group_settings (
	chat_id INTEGER NOT NULL,
	lifecycle VARCHAR(20) DEFAULT 'pending' NOT NULL,
	mode VARCHAR(12) DEFAULT 'observe' NOT NULL,
	health VARCHAR(16) DEFAULT 'healthy' NOT NULL,
	health_reason VARCHAR(100),
	version INTEGER DEFAULT '1' NOT NULL,
	policy JSONB DEFAULT '{}'::jsonb NOT NULL,
	last_dispatched_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (chat_id),
	CHECK (lifecycle IN ('pending','connected','archived','disconnected')),
	CHECK (mode IN ('observe','auto','paused')),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE TABLE inbox_updates (
	id UUID NOT NULL,
	bot_id BIGINT NOT NULL,
	update_id BIGINT NOT NULL,
	chat_id INTEGER,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	state VARCHAR(20) DEFAULT 'pending' NOT NULL,
	reason VARCHAR(80),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (bot_id, update_id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE TABLE incidents (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	incident_key VARCHAR(180) NOT NULL,
	user_id INTEGER,
	version INTEGER DEFAULT '1' NOT NULL,
	status VARCHAR(24) DEFAULT 'pending' NOT NULL,
	human_label VARCHAR(16),
	model_label VARCHAR(16),
	category VARCHAR(32),
	confidence FLOAT,
	reason TEXT,
	complete BOOLEAN DEFAULT 'true' NOT NULL,
	legacy BOOLEAN DEFAULT 'false' NOT NULL,
	active_strike BOOLEAN DEFAULT 'false' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (chat_id, incident_key),
	UNIQUE (chat_id, id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)
-- statement boundary
CREATE INDEX ix_incidents_chat_id_status_created_at_id ON incidents (chat_id, status, created_at, id)
-- statement boundary
CREATE TABLE jobs (
	id UUID NOT NULL,
	chat_id INTEGER,
	kind VARCHAR(32) NOT NULL,
	key VARCHAR(220) NOT NULL,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	priority INTEGER DEFAULT '10' NOT NULL,
	state VARCHAR(16) DEFAULT 'pending' NOT NULL,
	attempt INTEGER DEFAULT '0' NOT NULL,
	lease_token UUID,
	lease_until TIMESTAMP WITH TIME ZONE,
	due_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	last_error VARCHAR(100),
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	UNIQUE (key)
)
-- statement boundary
CREATE INDEX ix_jobs_claim ON jobs (state, due_at, priority, chat_id)
-- statement boundary
CREATE TABLE message_items (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	telegram_message_id BIGINT NOT NULL,
	user_id INTEGER,
	sender_chat_id BIGINT,
	author_kind VARCHAR(12) NOT NULL,
	revision INTEGER DEFAULT '1' NOT NULL,
	media_group_id VARCHAR(100),
	incident_key VARCHAR(180) NOT NULL,
	telegram_date TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	first_received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	edited_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (chat_id, telegram_message_id),
	UNIQUE (chat_id, id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)
-- statement boundary
CREATE INDEX ix_message_items_chat_id_telegram_date_id ON message_items (chat_id, telegram_date, id)
-- statement boundary
CREATE INDEX ix_message_items_chat_id_user_id_telegram_date_id ON message_items (chat_id, user_id, telegram_date, id)
-- statement boundary
CREATE TABLE onboarding_selections (
	request_id SERIAL NOT NULL,
	request UUID NOT NULL,
	actor_id BIGINT NOT NULL,
	side VARCHAR(8) NOT NULL,
	token_hash VARCHAR(64),
	attempts INTEGER DEFAULT '0' NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (request_id),
	FOREIGN KEY(request) REFERENCES onboarding_requests (id)
)
-- statement boundary
CREATE TABLE outbox (
	id UUID NOT NULL,
	chat_id INTEGER,
	destination BIGINT NOT NULL,
	binding_version INTEGER,
	kind VARCHAR(24) NOT NULL,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	key VARCHAR(220) NOT NULL,
	state VARCHAR(16) DEFAULT 'pending' NOT NULL,
	telegram_message_id BIGINT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	UNIQUE (key)
)
-- statement boundary
CREATE TABLE role_grants (
	id UUID NOT NULL,
	chat_id INTEGER,
	actor_id BIGINT NOT NULL,
	role VARCHAR(16) NOT NULL,
	granted_by BIGINT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CHECK ((role='owner' AND chat_id IS NULL) OR (role IN ('manager','moderator','observer') AND chat_id IS NOT NULL)),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE UNIQUE INDEX uq_local_role ON role_grants (chat_id, actor_id) WHERE revoked_at IS NULL AND chat_id IS NOT NULL
-- statement boundary
CREATE UNIQUE INDEX uq_owner_role ON role_grants (actor_id) WHERE revoked_at IS NULL AND chat_id IS NULL
-- statement boundary
CREATE TABLE trust_grants (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	source VARCHAR(24) NOT NULL,
	reason TEXT NOT NULL,
	actor_id BIGINT,
	cycle INTEGER,
	threshold INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)
-- statement boundary
CREATE UNIQUE INDEX uq_active_trust ON trust_grants (chat_id, user_id) WHERE revoked_at IS NULL
-- statement boundary
CREATE TABLE ui_operations (
	token VARCHAR(40) NOT NULL,
	actor_id BIGINT NOT NULL,
	admin_chat_id BIGINT NOT NULL,
	chat_id INTEGER NOT NULL,
	binding_version INTEGER NOT NULL,
	object_version INTEGER,
	kind VARCHAR(32) NOT NULL,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	result JSONB DEFAULT '{}'::jsonb NOT NULL,
	PRIMARY KEY (token),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE TABLE ui_sessions (
	actor_id BIGINT NOT NULL,
	admin_chat_id BIGINT NOT NULL,
	chat_id INTEGER,
	state JSONB DEFAULT '{}'::jsonb NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (actor_id, admin_chat_id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE TABLE admin_cards (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	incident_id UUID NOT NULL,
	admin_chat_id BIGINT NOT NULL,
	message_id BIGINT NOT NULL,
	binding_version INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (admin_chat_id, message_id),
	FOREIGN KEY(chat_id, incident_id) REFERENCES incidents (chat_id, id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE TABLE decisions (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	incident_id UUID NOT NULL,
	actor_id BIGINT,
	version INTEGER NOT NULL,
	label VARCHAR(16) NOT NULL,
	reason TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id, incident_id) REFERENCES incidents (chat_id, id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE TABLE member_states (
	chat_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	legacy_balance INTEGER DEFAULT '0' NOT NULL,
	balance INTEGER DEFAULT '0' NOT NULL,
	legacy_last_strike_at TIMESTAMP WITH TIME ZONE,
	version INTEGER DEFAULT '1' NOT NULL,
	cycle INTEGER DEFAULT '0' NOT NULL,
	cycle_started_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT uq_member_state UNIQUE (chat_id, user_id),
	FOREIGN KEY(chat_id, user_id) REFERENCES chat_members (chat_id, user_id),
	CHECK (balance >= 0),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)
-- statement boundary
CREATE TABLE message_revisions (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	item_id UUID NOT NULL,
	revision INTEGER NOT NULL,
	update_id BIGINT,
	text TEXT NOT NULL,
	normalized TEXT DEFAULT '' NOT NULL,
	ocr_text TEXT DEFAULT '' NOT NULL,
	content JSONB DEFAULT '{}'::jsonb NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	complete BOOLEAN DEFAULT 'true' NOT NULL,
	error VARCHAR(100),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (item_id, revision),
	UNIQUE (chat_id, id),
	FOREIGN KEY(chat_id, item_id) REFERENCES message_items (chat_id, id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE TABLE strike_events (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	incident_id UUID,
	delta INTEGER NOT NULL,
	kind VARCHAR(24) NOT NULL,
	actor_id BIGINT,
	reason TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id, incident_id) REFERENCES incidents (chat_id, id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)
-- statement boundary
CREATE UNIQUE INDEX uq_active_incident_strike ON strike_events (incident_id) WHERE revoked_at IS NULL AND kind = 'incident'
-- statement boundary
CREATE TABLE telegram_actions (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	incident_id UUID,
	user_id INTEGER,
	kind VARCHAR(20) NOT NULL,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	key VARCHAR(220) NOT NULL,
	actor_id BIGINT,
	binding_version INTEGER NOT NULL,
	object_version INTEGER,
	policy_version INTEGER NOT NULL,
	state VARCHAR(16) DEFAULT 'planned' NOT NULL,
	error VARCHAR(100),
	before JSONB DEFAULT '{}'::jsonb NOT NULL,
	result JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id, incident_id) REFERENCES incidents (chat_id, id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	UNIQUE (key)
)
-- statement boundary
CREATE TABLE trust_credits (
	chat_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	cycle INTEGER NOT NULL,
	incident_id UUID NOT NULL,
	eligible BOOLEAN NOT NULL,
	source VARCHAR(32) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	UNIQUE (chat_id, user_id, cycle, incident_id),
	FOREIGN KEY(chat_id, incident_id) REFERENCES incidents (chat_id, id),
	FOREIGN KEY(chat_id) REFERENCES chats (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)
-- statement boundary
CREATE INDEX ix_trust_credits_chat_id_user_id_cycle ON trust_credits (chat_id, user_id, cycle)
-- statement boundary
CREATE TABLE checks (
	id UUID NOT NULL,
	chat_id INTEGER NOT NULL,
	revision_id UUID NOT NULL,
	incident_id UUID NOT NULL,
	model_requested VARCHAR(100),
	model_reported VARCHAR(100),
	prompt_version VARCHAR(64),
	policy_version INTEGER,
	status VARCHAR(24) NOT NULL,
	label VARCHAR(16),
	category VARCHAR(32),
	confidence FLOAT,
	reason TEXT,
	usage JSONB DEFAULT '{}'::jsonb NOT NULL,
	cost_usd FLOAT,
	latency_ms INTEGER,
	source VARCHAR(32) NOT NULL,
	error VARCHAR(100),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(chat_id, revision_id) REFERENCES message_revisions (chat_id, id),
	FOREIGN KEY(chat_id, incident_id) REFERENCES incidents (chat_id, id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
-- statement boundary
CREATE INDEX ix_checks_chat_id_created_at_id ON checks (chat_id, created_at, id)
-- statement boundary
CREATE TABLE legacy_message_links (
	legacy_message_id INTEGER NOT NULL,
	chat_id INTEGER NOT NULL,
	item_id UUID NOT NULL,
	revision_id UUID NOT NULL,
	incident_id UUID NOT NULL,
	source_hash VARCHAR(64) NOT NULL,
	PRIMARY KEY (legacy_message_id),
	FOREIGN KEY(chat_id, item_id) REFERENCES message_items (chat_id, id),
	FOREIGN KEY(chat_id, revision_id) REFERENCES message_revisions (chat_id, id),
	FOREIGN KEY(chat_id, incident_id) REFERENCES incidents (chat_id, id),
	FOREIGN KEY(legacy_message_id) REFERENCES messages (id),
	FOREIGN KEY(chat_id) REFERENCES chats (id)
)
