using System;
using System.Net.Http;
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
            await shipment.DispatchAsync(DateTime.UtcNow, _carrier);
            _shipments.Save(shipment);
        }

        public void Deliver(string shipmentId)
        {
            var shipment = _shipments.Get(shipmentId);
            shipment.Deliver();
            _shipments.Save(shipment);
        }
    }
}
