package com.example.shop;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.Random;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * Fills an empty database with demo data: 50 products, 5,000 customers (fake but realistic
 * personal data), 20,000 reviews and 300,000 past orders.
 */
@Component
public class DataSeeder implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(DataSeeder.class);

    private final JdbcTemplate jdbc;
    private final String commit;

    public DataSeeder(JdbcTemplate jdbc, @Value("${app.commit}") String commit) {
        this.jdbc = jdbc;
        this.commit = commit;
    }

    @Override
    public void run(ApplicationArguments args) throws IOException {
        Integer products = jdbc.queryForObject("SELECT COUNT(*) FROM product", Integer.class);
        if (products != null && products > 0) {
            return;
        }
        log.info("seeding demo data");
        jdbc.update("""
                INSERT INTO product (id, name, description, price, stock)
                SELECT X, 'Handloom Item ' || X, REPEAT('Handwoven silk with a zari border. ', 160),
                       500 + MOD(X * 37, 14500), 1000000000
                FROM SYSTEM_RANGE(1, 50)""");
        seedCustomers(5000);
        jdbc.update("""
                INSERT INTO review (product_id, rating, body)
                SELECT MOD(X, 50) + 1, MOD(X, 5) + 1, 'Lovely fabric' FROM SYSTEM_RANGE(1, 20000)""");
        jdbc.update("""
                INSERT INTO orders (id, customer_id, created_at, total, status)
                SELECT X, MOD(X, 5000) + 1, DATEADD('MINUTE', -X, CURRENT_TIMESTAMP), 1000, 'PLACED'
                FROM SYSTEM_RANGE(1, 300000)""");
        jdbc.update("""
                INSERT INTO order_item (order_id, product_id, quantity, price)
                SELECT X, MOD(X, 50) + 1, 1, 1000 FROM SYSTEM_RANGE(1, 300000)""");
        jdbc.execute("ALTER TABLE product ALTER COLUMN id RESTART WITH 51");
        jdbc.execute("ALTER TABLE customer ALTER COLUMN id RESTART WITH 5001");
        jdbc.execute("ALTER TABLE orders ALTER COLUMN id RESTART WITH 300001");
        log.info("seed complete");
    }

    private void seedCustomers(int count) throws IOException {
        Properties people = new Properties();
        try (InputStream in = getClass().getResourceAsStream("/fake-people.properties")) {
            people.load(in);
        }
        String[] first = people.getProperty("firstNames").split(",");
        String[] last = people.getProperty("lastNames").split(",");
        String[] cities = people.getProperty("cities").split(",");
        String[] streets = people.getProperty("streets").split(",");
        Random rng = new Random(7);
        List<Object[]> rows = new ArrayList<>();
        for (int id = 1; id <= count; id++) {
            String f = first[rng.nextInt(first.length)];
            String l = last[rng.nextInt(last.length)];
            boolean hasAddress = rng.nextInt(10) >= 3;  // 30% have no address yet
            rows.add(new Object[] {
                    id, f + " " + l, (f + "." + l + id + "@example.com").toLowerCase(),
                    String.format("98%03d %05d", rng.nextInt(1000), rng.nextInt(100000)),
                    hasAddress ? cities[rng.nextInt(cities.length)] : null,
                    hasAddress ? (rng.nextInt(200) + 1) + " " + streets[rng.nextInt(streets.length)] : null});
        }
        jdbc.batchUpdate("INSERT INTO customer (id, name, email, phone, city, street) VALUES (?, ?, ?, ?, ?, ?)", rows);
    }

    @EventListener(ApplicationReadyEvent.class)
    public void started() {
        log.info("service started commit={} pid={}", commit, ProcessHandle.current().pid());
    }
}
