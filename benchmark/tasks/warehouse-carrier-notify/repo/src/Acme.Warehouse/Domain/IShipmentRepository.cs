namespace Acme.Warehouse.Domain
{
    public interface IShipmentRepository
    {
        Shipment Get(string id);

        void Save(Shipment shipment);
    }
}
