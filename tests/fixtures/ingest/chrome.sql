CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, last_visit_time INTEGER);
INSERT INTO urls VALUES
 (1,'https://www.imdb.com/title/tt0133093/','The Matrix (1999) - IMDb',3,13436154000000000),
 (2,'https://www.themoviedb.org/tv/1419-castle','Castle - The Movie Database (TMDB)',2,13436154000000000),
 (3,'https://letterboxd.com/film/arrival/','Arrival',1,13436154000000000),
 (4,'https://trakt.tv/shows/the-office-2005','The Office',4,13436154000000000),
 (5,'https://www.netflix.com/title/70140425','Watch Castle | Netflix',2,13436154000000000),
 (6,'https://en.wikipedia.org/wiki/Arrival_(film)','Arrival - Wikipedia',1,13436154000000000),
 (7,'https://bank.invalid/account/private','DO NOT READ OR RETAIN',100,13436154000000000),
 (8,'https://imdb.com.attacker.invalid/title/tt0133093/','DO NOT READ SPOOF',1,13436154000000000);
