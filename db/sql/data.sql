-- Demo data. Random values use a fixed seed, so everyone gets the same database.
select setseed(0.42);

insert into authors (name, country, born) values
    ('Leo Tolstoy',             'Russia',         1828),
    ('Fyodor Dostoevsky',       'Russia',         1821),
    ('Anton Chekhov',           'Russia',         1860),
    ('Mikhail Bulgakov',        'Russia',         1891),
    ('Ivo Andrić',              'Yugoslavia',     1892),
    ('Milorad Pavić',           'Serbia',         1929),
    ('Meša Selimović',          'Yugoslavia',     1910),
    ('Gabriel García Márquez',  'Colombia',       1927),
    ('George Orwell',           'United Kingdom', 1903),
    ('Ray Bradbury',            'USA',            1920),
    ('Ursula K. Le Guin',       'USA',            1929),
    ('Stanisław Lem',           'Poland',         1921),
    ('Haruki Murakami',         'Japan',          1949),
    ('Umberto Eco',             'Italy',          1932),
    ('Franz Kafka',             'Austria-Hungary', 1883);

insert into books (author_id, title, year, genre, price) values
    (1,  'War and Peace',                   1869, 'novel',        14.50),
    (1,  'Anna Karenina',                   1877, 'novel',        11.90),
    (2,  'Crime and Punishment',            1866, 'novel',         8.90),
    (2,  'The Brothers Karamazov',          1880, 'novel',        12.90),
    (2,  'The Idiot',                       1869, 'novel',         9.50),
    (3,  'The Cherry Orchard',              1904, 'play',          4.20),
    (3,  'Short Stories',                   1899, 'short stories', 5.60),
    (4,  'The Master and Margarita',        1967, 'novel',         9.90),
    (4,  'Heart of a Dog',                  1925, 'novella',       4.80),
    (5,  'The Bridge on the Drina',         1945, 'novel',         8.70),
    (5,  'The Damned Yard',                 1954, 'novella',       5.30),
    (6,  'Dictionary of the Khazars',       1984, 'novel',         9.10),
    (6,  'Landscape Painted with Tea',      1988, 'novel',         8.80),
    (7,  'Death and the Dervish',           1966, 'novel',         8.20),
    (8,  'One Hundred Years of Solitude',   1967, 'novel',         9.90),
    (8,  'Love in the Time of Cholera',     1985, 'novel',         9.40),
    (9,  'Nineteen Eighty-Four',            1949, 'dystopia',      6.50),
    (9,  'Animal Farm',                     1945, 'novella',       3.90),
    (10, 'Fahrenheit 451',                  1953, 'dystopia',      5.90),
    (10, 'The Martian Chronicles',          1950, 'science fiction', 6.40),
    (11, 'A Wizard of Earthsea',            1968, 'fantasy',       7.10),
    (11, 'The Left Hand of Darkness',       1969, 'science fiction', 7.60),
    (12, 'Solaris',                         1961, 'science fiction', 6.90),
    (12, 'The Cyberiad',                    1965, 'science fiction', 7.20),
    (13, 'Norwegian Wood',                  1987, 'novel',         8.30),
    (13, 'Kafka on the Shore',              2002, 'novel',         9.80),
    (14, 'The Name of the Rose',            1980, 'mystery',      10.50),
    (14, 'Foucault''s Pendulum',            1988, 'novel',        11.20),
    (15, 'The Trial',                       1925, 'novel',         6.10),
    (15, 'The Metamorphosis',               1915, 'novella',       3.50);

insert into customers (name, email, city, created_at)
select first || ' ' || last,
       'user' || g || '@example.com',
       (array['Novi Sad','Belgrade','Niš','Subotica','Kragujevac','Zagreb','Ljubljana'])[1 + floor(random() * 7)::int],
       now() - (random() * interval '730 days')
from generate_series(1, 500) as g,
     lateral (select (array['Ana','Marko','Jelena','Nikola','Ivan','Marija','Stefan','Olga','Dušan','Milica'])[1 + floor(random() * 10)::int] as first,
                     (array['Petrović','Ivanović','Jović','Smith','Nikolić','Kovač','Marković','Popović'])[1 + floor(random() * 8)::int] as last
              where g > 0) as n;

insert into orders (customer_id, created_at, status)
select 1 + floor(random() * 500)::int,
       now() - (random() * interval '365 days'),
       (array['new','paid','paid','shipped','shipped','shipped','cancelled'])[1 + floor(random() * 7)::int]
from generate_series(1, 5000);

insert into order_items (order_id, book_id, qty, price)
select o.id, b.id, 1 + floor(random() * 3)::int, b.price
from orders o
cross join lateral (
    select id, price from books
    where o.id > 0  -- refers to o, so the subquery runs again for every row
    order by random()
    limit 1 + floor(random() * 3)::int
) b;

analyze;
