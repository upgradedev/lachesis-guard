using System;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

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
        }

        public async Task DispatchAsync(DateTime now, HttpClient carrier)
        {
            Dispatch(now);
            var json = JsonSerializer.Serialize(new { shipmentId = Id, destination = Destination, weightKg = WeightKg });
            using var response = await carrier.PostAsync("v1/dispatches", new StringContent(json, Encoding.UTF8, "application/json"));
            response.EnsureSuccessStatusCode();
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
