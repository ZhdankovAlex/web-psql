-- Demo bookstore database: a few related tables, indexes and a view.

create table authors (
    id         serial primary key,
    name       text not null,
    country    text not null,
    born       int
);

create table books (
    id         serial primary key,
    author_id  int  not null references authors(id),
    title      text not null,
    year       int  not null,
    genre      text not null,
    price      numeric(8,2) not null check (price > 0)
);
create index books_author_idx on books(author_id);
create index books_genre_idx  on books(genre);

create table customers (
    id         serial primary key,
    name       text not null,
    email      text not null unique,
    city       text not null,
    created_at timestamptz not null default now()
);

create table orders (
    id          serial primary key,
    customer_id int not null references customers(id),
    created_at  timestamptz not null,
    status      text not null check (status in ('new', 'paid', 'shipped', 'cancelled'))
);
create index orders_customer_idx on orders(customer_id);
create index orders_created_idx  on orders(created_at);

create table order_items (
    order_id int not null references orders(id) on delete cascade,
    book_id  int not null references books(id),
    qty      int not null check (qty > 0),
    price    numeric(8,2) not null,
    primary key (order_id, book_id)
);

create view top_authors as
select a.name                      as author,
       count(distinct oi.order_id) as orders,
       sum(oi.qty)                 as copies,
       sum(oi.qty * oi.price)      as revenue
from authors a
join books b        on b.author_id = a.id
join order_items oi on oi.book_id = b.id
join orders o       on o.id = oi.order_id and o.status <> 'cancelled'
group by a.name
order by revenue desc
limit 10;

comment on table authors     is 'Authors';
comment on table books       is 'Books in the catalogue';
comment on table customers   is 'Customers';
comment on table orders      is 'Orders';
comment on table order_items is 'Order lines';
comment on view  top_authors is 'Ten authors with the highest revenue';
