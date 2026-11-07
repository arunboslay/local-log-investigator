package com.example.shop.web;

import org.hibernate.resource.jdbc.spi.StatementInspector;

/** Counts SQL statements Hibernate runs on the current request thread. */
public class QueryCounter implements StatementInspector {

    private static final ThreadLocal<int[]> COUNT = ThreadLocal.withInitial(() -> new int[1]);

    public static void reset() {
        COUNT.get()[0] = 0;
    }

    public static int get() {
        return COUNT.get()[0];
    }

    @Override
    public String inspect(String sql) {
        COUNT.get()[0]++;
        return sql;
    }
}
