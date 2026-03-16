package com.acme.pricing;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;


public final class DiscountAllocator {

    public record LineItem(String sku, BigDecimal amount) {
        public LineItem {
            Objects.requireNonNull(sku, "sku");
            Objects.requireNonNull(amount, "amount");
        }
    }

    /**
     * Allocate {@code totalDiscount} across line items proportionally by amount.
     *
     * @return map of sku -> allocated discount (scale=2)
     */
    public Map<String, BigDecimal> allocate(List<LineItem> items, BigDecimal totalDiscount) {
        Objects.requireNonNull(items, "items");
        Objects.requireNonNull(totalDiscount, "totalDiscount");

        Map<String, BigDecimal> out = new LinkedHashMap<>();
        if (items.isEmpty() || totalDiscount.signum() <= 0) {
            for (LineItem li : items) {
                out.put(li.sku(), BigDecimal.ZERO.setScale(2, RoundingMode.HALF_UP));
            }
            return out;
        }

        BigDecimal totalAmount = BigDecimal.ZERO;
        for (LineItem li : items) {
            totalAmount = totalAmount.add(li.amount());
        }
        if (totalAmount.signum() <= 0) {
            // Nothing to allocate against; keep zeros.
            for (LineItem li : items) {
                out.put(li.sku(), BigDecimal.ZERO.setScale(2, RoundingMode.HALF_UP));
            }
            return out;
        }

        BigDecimal allocated = BigDecimal.ZERO;
        for (int i = 0; i < items.size(); i++) {
            LineItem li = items.get(i);

            BigDecimal share;
            if (i == items.size() - 1) {
                // Put the remainder on the last line.
                share = totalDiscount.subtract(allocated);
            } else {
                share = totalDiscount
                        .multiply(li.amount())
                        .divide(totalAmount, 10, RoundingMode.HALF_UP)
                        .setScale(2, RoundingMode.HALF_UP);
            }

 
            share = share.setScale(2, RoundingMode.HALF_UP);

            out.put(li.sku(), share);
            allocated = allocated.add(share);
        }

        return out;
    }
}