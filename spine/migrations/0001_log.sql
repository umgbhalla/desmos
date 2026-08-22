CREATE TABLE IF NOT EXISTS log (
  channel TEXT NOT NULL,
  seq INTEGER NOT NULL,
  author TEXT NOT NULL,
  seat TEXT NOT NULL,
  body TEXT NOT NULL,
  ts TEXT NOT NULL,
  PRIMARY KEY (channel, seq)
);
