CREATE TABLE watch_state (
 uuid TEXT, media_type TEXT, base_id TEXT, last_played_at INTEGER,
 played INTEGER, kind TEXT, favorite INTEGER, duration_ms INTEGER,
 position_ms INTEGER, rating INTEGER, likes INTEGER, dropped INTEGER,
 season INTEGER, episode INTEGER
);
INSERT INTO watch_state VALUES
 ('a1b2c3d4-device', 'series', 'tmdb:1419', 1791734400000, 1, 'episode', 1, 100, 100, 9, 1, 0, 1, 2),
 ('a1b2c3d4-device', 'series', 'tmdb:1419', 1791734300000, 1, 'episode', 0, 100, 100, NULL, 0, 0, 1, 1),
 ('a1b2c3d4-device', 'series', 'tmdb:1419', 1791734500000, 0, 'episode', 0, 100, 45, NULL, 0, 0, 2, 1),
 ('b1b2c3d4-device', 'movie', 'tmdb:603', 1791734400000, 1, 'movie', 1, 100, 99, 8, 1, 0, NULL, NULL),
 ('c1b2c3d4-device', 'movie', 'tmdb:604', 1791734600000, 1, 'movie', 1, 100, 99, 8, 1, 0, NULL, NULL),
 ('a1b2c3d4-device', 'series', 'tmdb:1420', 1791734000000, 1, 'episode', 0, 100, 30, 2, 0, 1, 1, 3),
 ('a1b2c3d4-device', 'series', 'tmdb:1420', 1791734000000, 0, 'episode', 0, 100, 30, NULL, 0, 0, 1, 2),
 ('a1b2c3d4-device', 'movie', 'tmdb:605', NULL, 0, 'movie', 0, 0, 0, NULL, NULL, 0, NULL, NULL);
