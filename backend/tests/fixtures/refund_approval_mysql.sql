CREATE TABLE refund_approvals (
  id BIGINT PRIMARY KEY,
  amount BIGINT NOT NULL,
  status VARCHAR(32) NOT NULL,
  created_at VARCHAR(64) NOT NULL
);
