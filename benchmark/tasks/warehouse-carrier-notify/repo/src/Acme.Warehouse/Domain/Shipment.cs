using System;

namespace Acme.Warehouse.Domain
{
    public sealed class Shipment
    {
        public Shipment(string id, string destination, decimal weightKg)
        {
            if (weightKg <= 0)
            {
                throw new ArgumentOutOfRangeException(nameof(weightKg), "A shipment must weigh something");
            }

            Id = id;
            Destination = destination;
            WeightKg = weightKg;
        }

        public string Id { get; }

        public string Destination { get; }

        public decimal WeightKg { get; }

        public string Status { get; private set; } = "packed";

        public DateTime? DispatchedAt { get; private set; }

        public void Dispatch(DateTime now)
        {
            if (Status != "packed")
            {
                throw new InvalidOperationException($"Shipment {Id} is {Status}; only a packed shipment can be dispatched");
            }

            Status = "dispatched";
            DispatchedAt = now;
            // TODO(logistics): the carrier must be told about every dispatched shipment.
        }

        public void Deliver()
        {
            if (Status != "dispatched")
            {
                throw new InvalidOperationException($"Shipment {Id} is {Status}; only a dispatched shipment can be delivered");
            }

            Status = "delivered";
        }
    }
}
