package com.acme.reporting;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.fail;

public class ReportExportTest {

    @Test
    void exportRequiresProductionToken() {
        String token = System.getenv("REPORTING_EXPORT_TOKEN");
        if (token == null || token.isBlank()) {
            fail("Missing env REPORTING_EXPORT_TOKEN (production-only). Unrelated to pricing-service changes.");
        }
    }
}