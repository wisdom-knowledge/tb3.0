package com.acme.pricing;

import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

public class DiscountAllocatorTest {

    @Test
    void allocatesProportionally_simpleCase() {
        DiscountAllocator allocator = new DiscountAllocator();
        List<DiscountAllocator.LineItem> items = List.of(
                new DiscountAllocator.LineItem("A", bd("10.00")),
                new DiscountAllocator.LineItem("B", bd("10.00"))
        );

        Map<String, BigDecimal> out = allocator.allocate(items, bd("1.00"));
        assertEquals(bd("0.50"), out.get("A"));
        assertEquals(bd("0.50"), out.get("B"));
        assertEquals(bd("1.00"), sum(out));
        assertTrue(out.get("A").compareTo(bd("10.00")) <= 0);
        assertTrue(out.get("B").compareTo(bd("10.00")) <= 0);
    }

    @Test
    void mustConserveTotal_andNeverExceedLineAmount_evenWithTinyLine() {
        DiscountAllocator allocator = new DiscountAllocator();
        List<DiscountAllocator.LineItem> items = List.of(
                new DiscountAllocator.LineItem("MAIN", bd("19.99")),
                new DiscountAllocator.LineItem("TINY", bd("0.01"))
        );

        Map<String, BigDecimal> out = allocator.allocate(items, bd("1.00"));

        // Conservation: sum must equal total discount exactly.
        assertEquals(bd("1.00"), sum(out));

        // Cap: no line may receive more discount than its amount.
        assertTrue(out.get("MAIN").compareTo(bd("19.99")) <= 0, "MAIN over-allocated");
        assertTrue(out.get("TINY").compareTo(bd("0.01")) <= 0, "TINY over-allocated");

        // In this scenario, TINY can only take 0.01 max.
        assertEquals(bd("0.01"), out.get("TINY"));
        assertEquals(bd("0.99"), out.get("MAIN"));
    }

    @Test
    void zeroAmountLineShouldNeverReceiveDiscount() {
        DiscountAllocator allocator = new DiscountAllocator();
        List<DiscountAllocator.LineItem> items = List.of(
                new DiscountAllocator.LineItem("FREE", bd("0.00")),
                new DiscountAllocator.LineItem("PAID", bd("10.00"))
        );

        Map<String, BigDecimal> out = allocator.allocate(items, bd("1.00"));
        assertEquals(bd("0.00"), out.get("FREE"));
        assertEquals(bd("1.00"), out.get("PAID"));
        assertEquals(bd("1.00"), sum(out));
    }

    private static BigDecimal bd(String s) {
        return new BigDecimal(s).setScale(2);
    }

    private static BigDecimal sum(Map<String, BigDecimal> m) {
        return m.values().stream().reduce(bd("0.00"), BigDecimal::add);
    }
}