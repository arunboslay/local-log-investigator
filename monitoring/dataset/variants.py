"""Fault variants for the per-request training data.

Several variants per fault family (different endpoints, fields, lines), so the model learns the
pattern rather than one bug. Whole families are held out from training to test whether it
generalizes to kinds of failure it has never seen.

Only faults that show up in individual requests are used here; memory leaks and traffic spikes
show up in metrics over time, not in one request's logs.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from faults.catalog import (APP_PROPERTIES, CUSTOMER_SERVICE, FAULTS, J, ORDER_SERVICE,  # noqa: E402
                            Fault, code_fault, ops_fault)

PRODUCT_REPOSITORY = J + "product/ProductRepository.java"
PRODUCT_SERVICE = J + "product/ProductService.java"

HELD_OUT_FAMILIES = {"n_plus_one", "pool_exhausted", "payment_down"}


@dataclass
class Variant:
    id: str
    family: str
    fault: Fault
    endpoints: list[str]  # "METHOD /pattern" of requests this fault hurts
    short_cause: str  # one sentence used in the labels
    marker: str | None = None  # text on the guilty line (code/config faults)
    marker_file: str | None = None


GIFT_FOOTER = ("\\n\\n-- Wrapped with care by the Demo Shop. Every saree is handwoven by artisans from Kanchipuram "
               "and Varanasi. Returns accepted within 30 days with the original tag. Visit us again for festive "
               "offers and new arrivals! --")

NEW_FAULTS = [
    code_fault(
        "silent_save_phone_suffix", "code_change",
        "A commit appends ' (mobile)' to every phone number, which overflows the 15-character PHONE column. "
        "The insert fails, CustomerService.signup swallows it and still returns 201.",
        ["POST /api/customers"], [],
        [(CUSTOMER_SERVICE, "        return phone == null ? null : phone.trim();\n",
          '        return phone == null ? null : phone.trim() + " (mobile)";\n')],
        "Label phone numbers as mobile", "CustomerService.normalizePhone"),
    code_fault(
        "silent_save_order", "code_change",
        "A commit adds a long footer to gift messages (over the 200-character GIFT_MESSAGE column) and wraps the "
        "order save in a try/catch that only logs a warning. Gift orders return 201 'PLACED' but are not saved.",
        ["POST /api/orders"], [],
        [(ORDER_SERVICE, "import org.springframework.stereotype.Service;\n",
          "import org.springframework.dao.DataAccessException;\nimport org.springframework.stereotype.Service;\n"),
         (ORDER_SERVICE, "    private static final Logger log = LoggerFactory.getLogger(OrderService.class);\n",
          "    private static final Logger log = LoggerFactory.getLogger(OrderService.class);\n"
          f'    private static final String GIFT_FOOTER = "{GIFT_FOOTER}";\n'),
         (ORDER_SERVICE, "        order.setGiftMessage(request.giftMessage());\n",
          "        order.setGiftMessage(request.giftMessage() == null ? null : request.giftMessage() + GIFT_FOOTER);\n"),
         (ORDER_SERVICE,
          "        Order saved = tx.execute(status -> {\n"
          "            productRepository.decrementStock(product.getId(), request.quantity());\n"
          "            return orderRepository.save(order);\n"
          "        });\n"
          '        log.info("order saved orderId={} total={}", saved.getId(), total);\n'
          "        return new OrderResponse(saved.getId(), saved.getStatus(), total);\n",
          "        Order saved = order;\n"
          "        try {\n"
          "            saved = tx.execute(status -> {\n"
          "                productRepository.decrementStock(product.getId(), request.quantity());\n"
          "                return orderRepository.save(order);\n"
          "            });\n"
          '            log.info("order saved orderId={} total={}", saved.getId(), total);\n'
          "        } catch (DataAccessException e) {\n"
          "            // Payment already went through; don't show the customer an error.\n"
          '            log.warn("could not save order customerId={}: {}", customer.getId(), e.getMostSpecificCause().getMessage());\n'
          "        }\n"
          '        return new OrderResponse(saved.getId(), "PLACED", total);\n')],
        "Add gift card footer; don't fail checkout after payment", "OrderService.buildOrder"),
    code_fault(
        "npe_customer_city", "code_change",
        "A commit logs the customer's city in capitals with customer.getCity().toUpperCase(); customers without "
        "an address have no city, so GET /api/customers/{id} fails with NullPointerException for them.",
        ["GET /api/customers/{id}"], [],
        [(CUSTOMER_SERVICE,
          '        Customer customer = customerRepository.findById(id).orElseThrow(() -> new NotFoundException("customer", id));\n'
          "        return CustomerResponse.of(customer);\n",
          '        Customer customer = customerRepository.findById(id).orElseThrow(() -> new NotFoundException("customer", id));\n'
          '        log.info("customer loaded customerId={} city={}", id, customer.getCity().toUpperCase());\n'
          "        return CustomerResponse.of(customer);\n")],
        "Log customer city on lookup", "CustomerService.getCustomer"),
    code_fault(
        "npe_signup_phone", "code_change",
        "A commit strips spaces from phone numbers with phone.trim().replace(...) but drops the null check, so "
        "signups without a phone fail with NullPointerException.",
        ["POST /api/customers"], [],
        [(CUSTOMER_SERVICE, "        return phone == null ? null : phone.trim();\n",
          '        return phone.trim().replace(" ", "");\n')],
        "Remove spaces from phone numbers", "CustomerService.normalizePhone"),
    code_fault(
        "bad_payment_url", "config_change",
        "A config commit points payment.base-url at port 8102, where nothing is listening, so every payment "
        "call is refused and POST /api/orders returns 502.",
        ["POST /api/orders"], [],
        [(APP_PROPERTIES, "payment.base-url=http://127.0.0.1:8101\n", "payment.base-url=http://127.0.0.1:8102\n")],
        "Switch to the new payment gateway", "application.properties payment.base-url"),
    code_fault(
        "slow_product_query", "code_change",
        "A commit makes product lookup a native query with 'p.id IN (SELECT product_id FROM order_item)'. "
        "order_item has no index on product_id, so every GET /api/products/{id} scans 300k rows.",
        ["GET /api/products/{id}"], [],
        [(PRODUCT_REPOSITORY, "import java.util.List;\n", "import java.util.List;\nimport java.util.Optional;\n"),
         (PRODUCT_REPOSITORY, "    @Modifying\n",
          "    // Only show products that have sold at least once.\n"
          '    @Query(value = "SELECT p.* FROM product p WHERE p.id = :id AND p.id IN (SELECT product_id FROM order_item)", nativeQuery = true)\n'
          '    Optional<Product> findSoldProduct(@Param("id") Long id);\n\n'
          "    @Modifying\n"),
         (PRODUCT_SERVICE, 'productRepository.findById(id).orElseThrow(() -> new NotFoundException("product", id));',
          'productRepository.findSoldProduct(id).orElseThrow(() -> new NotFoundException("product", id));')],
        "Only show products that have sold", "ProductRepository.findSoldProduct"),
]
_NEW = {f.id: f for f in NEW_FAULTS}


def _v(id, family, fault, endpoints, short_cause, marker=None, marker_file=None):
    return Variant(id, family, fault, endpoints, short_cause, marker, marker_file)


VARIANTS = [
    _v("silent_save_phone", "silent_save", FAULTS["silent_save_failure"], ["POST /api/customers"],
       "a recent change adds '+91-' to phone numbers, so some no longer fit the 15-character PHONE column; "
       "the insert fails and the error is caught and only logged",
       '"+91-" + phone.trim()', CUSTOMER_SERVICE),
    _v("silent_save_phone_suffix", "silent_save", _NEW["silent_save_phone_suffix"], ["POST /api/customers"],
       "a recent change appends ' (mobile)' to phone numbers, so they no longer fit the 15-character PHONE "
       "column; the insert fails and the error is caught and only logged",
       '" (mobile)"', CUSTOMER_SERVICE),
    _v("silent_save_order", "silent_save", _NEW["silent_save_order"], ["POST /api/orders"],
       "a recent change adds a long footer to gift messages, longer than the 200-character GIFT_MESSAGE "
       "column; the order insert fails and a try/catch hides it",
       "request.giftMessage() + GIFT_FOOTER", ORDER_SERVICE),
    _v("npe_gift_city", "npe", FAULTS["rare_npe"], ["POST /api/orders"],
       "customer.getCity() is null for customers without an address, and the gift message code calls "
       ".toUpperCase() on it", "customer.getCity().toUpperCase()", ORDER_SERVICE),
    _v("npe_customer_city", "npe", _NEW["npe_customer_city"], ["GET /api/customers/{id}"],
       "customer.getCity() is null for customers without an address, and the new log line calls "
       ".toUpperCase() on it", "customer.getCity().toUpperCase()", CUSTOMER_SERVICE),
    _v("npe_signup_phone", "npe", _NEW["npe_signup_phone"], ["POST /api/customers"],
       "normalizePhone no longer checks for a missing phone, so phone.trim() fails when the phone is null",
       'phone.trim().replace(" ", "")', CUSTOMER_SERVICE),
    _v("bad_timeout", "bad_config", FAULTS["bad_config_property"], ["POST /api/orders"],
       "payment.timeout-ms was lowered to 20 ms, shorter than a normal payment call", "payment.timeout-ms=20",
       APP_PROPERTIES),
    _v("bad_payment_url", "bad_config", _NEW["bad_payment_url"], ["POST /api/orders"],
       "payment.base-url points to port 8102, where no payment service is listening",
       "payment.base-url=http://127.0.0.1:8102", APP_PROPERTIES),
    _v("slow_order_query", "slow_query", FAULTS["slow_query"], ["GET /api/orders/{id}"],
       "the order lookup compares CAST(id AS VARCHAR), so the database can't use the primary key and scans "
       "every order", "CAST(id AS VARCHAR)", J + "order/OrderRepository.java"),
    _v("slow_product_query", "slow_query", _NEW["slow_product_query"], ["GET /api/products/{id}"],
       "the product lookup checks 'IN (SELECT product_id FROM order_item)' on a column with no index, so it "
       "scans 300k rows", "IN (SELECT product_id FROM order_item)", PRODUCT_REPOSITORY),
    _v("payment_timeout", "dependency", FAULTS["payment_timeout"], ["POST /api/orders"],
       "the outside payment service is responding slower than the 800 ms timeout"),
    _v("db_down", "database", FAULTS["db_down"],
       ["GET /api/products", "GET /api/products/{id}", "POST /api/customers", "PUT /api/customers/{id}/address",
        "GET /api/customers/{id}", "POST /api/orders", "GET /api/orders/{id}"],
       "the database server is not reachable"),
    # Held out: never trained on.
    _v("n_plus_one", "n_plus_one", FAULTS["n_plus_one"], ["GET /api/products"],
       "listProducts now loads each product's reviews one product at a time (an N+1 query: 1 + 50 queries)",
       "p.getReviews().size()", PRODUCT_SERVICE),
    _v("pool_exhausted", "pool_exhausted", FAULTS["pool_exhausted"],
       ["GET /api/products", "GET /api/products/{id}", "POST /api/customers", "PUT /api/customers/{id}/address",
        "GET /api/customers/{id}", "POST /api/orders", "GET /api/orders/{id}"],
       "OrderService.currentStock takes a database connection and never closes it, so the pool runs out",
       "Connection conn = dataSource.getConnection();", ORDER_SERVICE),
    _v("payment_down", "payment_down", FAULTS["payment_down"], ["POST /api/orders"],
       "the outside payment service is down and refuses connections"),
]
VARIANTS_BY_ID = {v.id: v for v in VARIANTS}
