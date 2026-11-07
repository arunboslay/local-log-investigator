"""The fault catalog: ways to break the demo shop, each with a way to undo it and the true cause.

Code and config faults are real git commits in demo_app followed by a build and deploy, so the
guilty commit shows up in the history like it would in real life. Undo is `git revert` plus a deploy.
"""

from dataclasses import dataclass
from typing import Callable

from lab import BASE_RPS, Lab

J = "src/main/java/com/example/shop/"
CUSTOMER_SERVICE = J + "customer/CustomerService.java"
PRODUCT_SERVICE = J + "product/ProductService.java"
ORDER_SERVICE = J + "order/OrderService.java"
ORDER_REPOSITORY = J + "order/OrderRepository.java"
APP_PROPERTIES = "src/main/resources/application.properties"


@dataclass
class Fault:
    id: str
    category: str  # code_change | config_change | database | dependency | traffic
    cause: str  # the true cause, in plain words
    endpoints: list[str]  # endpoints expected to suffer
    signals: list[str]  # what an investigator should be able to see
    inject: Callable[[Lab], dict]  # returns extra truth info, e.g. the guilty commit
    revert: Callable[[Lab, dict], None]
    code_location: str | None = None  # where the bug lives, for code faults


def code_fault(id, category, cause, endpoints, signals, edits, message, code_location) -> Fault:
    def inject(lab: Lab) -> dict:
        sha = lab.commit_edits(edits, message)
        lab.deploy()
        return {"guilty_commit": sha, "guilty_files": sorted({f for f, _, _ in edits})}

    def revert(lab: Lab, info: dict):
        lab.revert_commit(info["guilty_commit"])
        lab.deploy()

    return Fault(id, category, cause, endpoints, signals, inject, revert, code_location)


def ops_fault(id, category, cause, endpoints, signals, inject, revert) -> Fault:
    def wrapped_inject(lab: Lab) -> dict:
        inject(lab)
        return {"guilty_commit": None, "guilty_files": []}

    return Fault(id, category, cause, endpoints, signals, wrapped_inject, lambda lab, info: revert(lab))


CODE_FAULTS = [
    code_fault(
        "silent_save_failure", "code_change",
        "A commit prefixes phone numbers with '+91-'. Numbers that already start with '+91 ' become 19 "
        "characters, longer than the 15-character PHONE column, so the insert fails. CustomerService.signup "
        "catches the DataAccessException, logs a WARN and still returns 201 'Signup complete' with a null "
        "customerId. The user sees success, but the customer was never saved.",
        ["POST /api/customers"],
        ["no 5xx at all: POST /api/customers still returns 201",
         "WARN 'could not save customer ... Value too long for column PHONE' for about half of signups",
         "those requests have 'signup validated' and 'signup completed' but no 'customer saved'",
         "starts right after the deploy of the guilty commit"],
        [(CUSTOMER_SERVICE, "        return phone == null ? null : phone.trim();\n",
          '        return phone == null ? null : "+91-" + phone.trim();\n')],
        "Store phone numbers with India country code",
        "CustomerService.normalizePhone (bug) + CustomerService.signup catch block (hides it)",
    ),
    code_fault(
        "bad_config_property", "config_change",
        "A config commit lowered payment.timeout-ms from 800 to 20. Normal payment calls take 20-60 ms, so "
        "most time out and POST /api/orders returns 502. The payment service itself is healthy.",
        ["POST /api/orders"],
        ["502s on POST /api/orders", "WARN 'payment call failed ... timeoutMs=20'", "starts at deploy"],
        [(APP_PROPERTIES, "payment.timeout-ms=800\n", "payment.timeout-ms=20\n")],
        "Tighten payment timeout to fail fast",
        "application.properties payment.timeout-ms",
    ),
    code_fault(
        "slow_query", "code_change",
        "A commit changed order lookup to a native query comparing CAST(id AS VARCHAR). The database can no "
        "longer use the primary key, so every GET /api/orders/{id} scans 300k rows.",
        ["GET /api/orders/{id}"],
        ["latency up on GET /api/orders/{id} only", "no errors", "starts at deploy"],
        [(ORDER_REPOSITORY, "import org.springframework.data.jpa.repository.JpaRepository;\n",
          "import java.util.Optional;\n\n"
          "import org.springframework.data.jpa.repository.JpaRepository;\n"
          "import org.springframework.data.jpa.repository.Query;\n"
          "import org.springframework.data.repository.query.Param;\n"),
         (ORDER_REPOSITORY, "public interface OrderRepository extends JpaRepository<Order, Long> {\n}",
          "public interface OrderRepository extends JpaRepository<Order, Long> {\n\n"
          "    // Legacy systems send order references as text.\n"
          '    @Query(value = "SELECT * FROM orders WHERE CAST(id AS VARCHAR) = :ref", nativeQuery = true)\n'
          '    Optional<Order> findByReference(@Param("ref") String ref);\n}'),
         (ORDER_SERVICE, 'orderRepository.findById(id).orElseThrow(() -> new NotFoundException("order", id));',
          'orderRepository.findByReference(String.valueOf(id)).orElseThrow(() -> new NotFoundException("order", id));')],
        "Look up orders by reference so legacy text ids work",
        "OrderRepository.findByReference, called from OrderService.getOrder",
    ),
    code_fault(
        "n_plus_one", "code_change",
        "A commit replaced the single grouped query in ProductService.listProducts with findAll() plus "
        "p.getReviews().size() per product: an N+1 query (1 query for the list, then 1 per product, each "
        "loading all its reviews).",
        ["GET /api/products"],
        ["latency up on GET /api/products", "dbQueries per request jumps from 1 to 51", "starts at deploy"],
        [(PRODUCT_SERVICE, "import org.springframework.stereotype.Service;\n",
          "import org.springframework.data.domain.Sort;\nimport org.springframework.stereotype.Service;\n"),
         (PRODUCT_SERVICE, "        List<ProductSummary> products = productRepository.findSummaries();\n",
          '        List<ProductSummary> products = productRepository.findAll(Sort.by("id")).stream()\n'
          "                .map(p -> new ProductSummary(p.getId(), p.getName(), p.getPrice(), p.getStock(),\n"
          "                        p.getReviews().size()))\n"
          "                .toList();\n")],
        "Simplify product listing",
        "ProductService.listProducts",
    ),
    code_fault(
        "memory_leak", "code_change",
        "A commit added a static PAGE_CACHE in ProductService keyed by System.nanoTime(). Every GET "
        "/api/products adds all 50 products (with long descriptions) and nothing is removed, so heap use "
        "climbs, garbage collection gets busier, and the app slows down.",
        ["GET /api/products", "eventually all"],
        ["jvm_memory_used_bytes{area=heap} climbing steadily", "GC pause time up", "starts at deploy"],
        [(PRODUCT_SERVICE, "import java.util.List;\n",
          "import java.util.List;\nimport java.util.Map;\nimport java.util.concurrent.ConcurrentHashMap;\n"),
         (PRODUCT_SERVICE, "    private final ProductRepository productRepository;\n",
          "    private final ProductRepository productRepository;\n\n"
          "    // Storefront page cache: full product rows, reused by product pages.\n"
          "    private static final Map<Long, List<Product>> PAGE_CACHE = new ConcurrentHashMap<>();\n"),
         (PRODUCT_SERVICE, '        log.info("products listed count={}", products.size());\n',
          "        PAGE_CACHE.put(System.nanoTime(), productRepository.findAll());\n"
          '        log.info("products listed count={}", products.size());\n')],
        "Cache product pages for the storefront",
        "ProductService.PAGE_CACHE / ProductService.listProducts",
    ),
    code_fault(
        "pool_exhausted", "code_change",
        "A commit added OrderService.currentStock, which takes a JDBC connection from the pool and never "
        "closes it. After 10 orders the pool (size 10) is empty, and every database call waits 2 s and fails "
        "with 'Connection is not available'.",
        ["all endpoints that use the database"],
        ["Hikari WARN 'Connection leak detection triggered' with a stack trace through OrderService.currentStock",
         "then 500s everywhere: 'HikariPool-1 - Connection is not available, request timed out after 2000ms'",
         "hikaricp_connections_active stuck at 10", "starts at deploy"],
        [(ORDER_SERVICE, "import java.math.BigDecimal;\n",
          "import java.math.BigDecimal;\nimport java.sql.Connection;\nimport java.sql.PreparedStatement;\n"
          "import java.sql.ResultSet;\nimport java.sql.SQLException;\n\nimport javax.sql.DataSource;\n"),
         (ORDER_SERVICE, "import org.springframework.stereotype.Service;\n",
          "import org.springframework.beans.factory.annotation.Autowired;\nimport org.springframework.stereotype.Service;\n"),
         (ORDER_SERVICE, "    private final TransactionTemplate tx;\n",
          "    private final TransactionTemplate tx;\n\n    @Autowired\n    private DataSource dataSource;\n"),
         (ORDER_SERVICE, "        if (product.getStock() < request.quantity()) {\n",
          "        if (currentStock(product.getId()) < request.quantity()) {\n"),
         (ORDER_SERVICE, "    private Order buildOrder(",
          "    private int currentStock(Long productId) {\n"
          "        try {\n"
          "            Connection conn = dataSource.getConnection();\n"
          '            PreparedStatement ps = conn.prepareStatement("SELECT stock FROM product WHERE id = ?");\n'
          "            ps.setLong(1, productId);\n"
          "            ResultSet rs = ps.executeQuery();\n"
          "            rs.next();\n"
          "            return rs.getInt(1);\n"
          "        } catch (SQLException e) {\n"
          '            throw new IllegalStateException("stock lookup failed", e);\n'
          "        }\n"
          "    }\n\n"
          "    private Order buildOrder(")],
        "Read live stock from the database before charging",
        "OrderService.currentStock (connection never closed)",
    ),
    code_fault(
        "rare_npe", "code_change",
        "A commit appends the delivery city to gift messages with customer.getCity().toUpperCase(). Customers "
        "without an address have no city, so gift orders from them fail with NullPointerException. Only a few "
        "percent of orders hit this branch.",
        ["POST /api/orders"],
        ["occasional 500s on POST /api/orders", "NullPointerException in OrderService.buildOrder",
         "failing requests had a gift message", "starts at deploy"],
        [(ORDER_SERVICE, "        order.setGiftMessage(request.giftMessage());\n",
          "        if (request.giftMessage() != null) {\n"
          '            order.setGiftMessage(request.giftMessage() + " - deliver to " + customer.getCity().toUpperCase());\n'
          "        }\n")],
        "Add delivery city to gift messages",
        "OrderService.buildOrder",
    ),
]


OPS_FAULTS = [
    ops_fault(
        "payment_timeout", "dependency",
        "The outside payment service became slow (+1.5 s per call), longer than the 800 ms timeout, so "
        "POST /api/orders returns 502. No code or config changed.",
        ["POST /api/orders"],
        ["502s on POST /api/orders", "WARN 'payment call failed ... elapsedMs≈800 timeoutMs=800'", "no deploy"],
        lambda lab: lab.write_control("payment", {"delay_ms": 1500}),
        lambda lab: lab.write_control("payment", {"delay_ms": 0}),
    ),
    ops_fault(
        "payment_down", "dependency",
        "The outside payment service is down; connections are refused, so POST /api/orders returns 502.",
        ["POST /api/orders"],
        ["502s on POST /api/orders", "payment call failed quickly (connection refused)", "no deploy"],
        lambda lab: lab.stop("payment"),
        lambda lab: lab.start_payment(),
    ),
    ops_fault(
        "db_down", "database",
        "The database server went down. Every request that needs the database fails.",
        ["all endpoints that use the database"],
        ["500s on nearly everything", "connection errors to localhost:9092 in stack traces",
         "/actuator/health DOWN", "no deploy"],
        lambda lab: lab.stop("h2"),
        lambda lab: lab.start_db(),
    ),
    ops_fault(
        "traffic_spike", "traffic",
        f"Traffic jumped from {BASE_RPS} to 150 requests per second. The app slows down under the load; "
        "no code or config changed.",
        ["all"],
        ["request rate up ~15x", "latency up on all endpoints", "no deploy"],
        lambda lab: lab.write_control("load", {"rps": 150}),
        lambda lab: lab.write_control("load", {"rps": BASE_RPS}),
    ),
]

FAULTS = {f.id: f for f in CODE_FAULTS + OPS_FAULTS}
