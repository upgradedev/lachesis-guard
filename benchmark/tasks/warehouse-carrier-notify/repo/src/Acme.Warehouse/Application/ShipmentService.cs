using System;
using System.Threading.Tasks;
using Acme.Warehouse.Domain;

namespace Acme.Warehouse.Application
{
    public sealed class ShipmentService
    {
        private readonly IShipmentRepository _shipments;

        public ShipmentService(IShipmentRepository shipments)
        {
            _shipments = shipments;
        }

        public Task DispatchAsync(string shipmentId)
        {
            var shipment = _shipments.Get(shipmentId);
            shipment.Dispatch(DateTime.UtcNow);
            _shipments.Save(shipment);
            return Task.CompletedTask;
        }

        public void Deliver(string shipmentId)
        {
            var shipment = _shipments.Get(shipmentId);
            shipment.Deliver();
            _shipments.Save(shipment);
        }
    }
}
