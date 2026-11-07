package com.example.shop.product;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface ProductRepository extends JpaRepository<Product, Long> {

    @Query("""
            select new com.example.shop.product.ProductSummary(p.id, p.name, p.price, p.stock, count(r))
            from Product p left join p.reviews r
            group by p.id, p.name, p.price, p.stock
            order by p.id""")
    List<ProductSummary> findSummaries();

    @Modifying
    @Query("update Product p set p.stock = p.stock - :quantity where p.id = :id")
    int decrementStock(@Param("id") Long id, @Param("quantity") int quantity);
}
