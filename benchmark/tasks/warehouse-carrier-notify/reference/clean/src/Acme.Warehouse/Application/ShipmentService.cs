using System;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using Acme.Warehouse.Domain;

namespace Acme.Warehouse.Application
{
    public sealed class ShipmentService
    {
        private readonly IShipmentRepository _shipments;
        private readonly HttpClient _carrier;

        public ShipmentService(IShipmentRepository shipments, HttpClient carrier)
        {
            _shipments = shipments;
            _carrier = carrier;
        }

        public async Task DispatchAsync(string shipmentId)
        {
            var shipment = _shipments.Get(shipmentId);
            shipment.Dispatch(DateTime.UtcNow);
            _shipments.Save(shipment);

            var json = JsonSerializer.Serialize(new
            {
                shipmentId = shipment.Id,
                destination = shipment.Destination,
                weightKg = shipment.WeightKg,
            });
            using var response = await _carrier.PostAsync("v1/dispatches", new StringContent(json, Encoding.UTF8, "application/json"));
            response.EnsureSuccessStatusCode();
        }

        public void Deliver(string shipmentId)
        {
            var shipment = _shipments.Get(shipmentId);
            shipment.Deliver();
            _shipments.Save(shipment);
        }
    }
}
