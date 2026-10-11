using System;
using Acme.Warehouse.Domain;

namespace Acme.Warehouse.Tests
{
    public static class ShipmentTests
    {
        [Test]
        public static void A_packed_shipment_is_dispatched_once()
        {
            var shipment = new Shipment("S-1", "Rotterdam", 12.5m);
            shipment.Dispatch(new DateTime(2026, 9, 1, 8, 0, 0, DateTimeKind.Utc));
            Check.Equal("dispatched", shipment.Status, "status");
            Check.True(shipment.DispatchedAt.HasValue, "the dispatch time is recorded");

            var again = false;
            try
            {
                shipment.Dispatch(DateTime.UtcNow);
            }
            catch (InvalidOperationException)
            {
                again = true;
            }

            Check.True(again, "a shipment cannot be dispatched twice");
        }

        [Test]
        public static void Only_a_dispatched_shipment_is_delivered()
        {
            var shipment = new Shipment("S-2", "Lyon", 3m);
            var refused = false;
            try
            {
                shipment.Deliver();
            }
            catch (InvalidOperationException)
            {
                refused = true;
            }

            Check.True(refused, "a packed shipment cannot be delivered");
        }
    }
}
