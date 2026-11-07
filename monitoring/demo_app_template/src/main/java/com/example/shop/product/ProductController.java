package com.example.shop.product;

import java.math.BigDecimal;
import java.util.List;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/products")
public class ProductController {

    private final ProductService productService;

    public ProductController(ProductService productService) {
        this.productService = productService;
    }

    @GetMapping
    public List<ProductSummary> listProducts() {
        return productService.listProducts();
    }

    @GetMapping("/{id}")
    public ProductDetail getProduct(@PathVariable Long id) {
        Product p = productService.getProduct(id);
        return new ProductDetail(p.getId(), p.getName(), p.getDescription(), p.getPrice(), p.getStock());
    }

    public record ProductDetail(Long id, String name, String description, BigDecimal price, int stock) {
    }
}
