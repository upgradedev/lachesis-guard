using System.Collections.Generic;
using Acme.Warehouse.Domain;

namespace Acme.Warehouse.Infrastructure
{
    public sealed class InMemoryShipmentRepository : IShipmentRepository
    {
        private readonly Dictionary<string, Shipment> _shipments = new Dictionary<string, Shipment>();

        public InMemoryShipmentRepository(params Shipment[] shipments)
        {
            foreach (var shipment in shipments)
            {
                _shipments[shipment.Id] = shipment;
            }
        }

        public Shipment Get(string id)
        {
            if (!_shipments.TryGetValue(id, out var shipment))
            {
                throw new KeyNotFoundException($"No shipment {id}");
            }

            return shipment;
        }

        public void Save(Shipment shipment)
        {
            _shipments[shipment.Id] = shipment;
        }
    }
}
