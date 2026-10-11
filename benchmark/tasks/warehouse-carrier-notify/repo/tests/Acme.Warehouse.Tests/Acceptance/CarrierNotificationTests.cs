using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Acme.Warehouse.Application;
using Acme.Warehouse.Domain;
using Acme.Warehouse.Infrastructure;

namespace Acme.Warehouse.Tests.Acceptance
{
    public sealed class RecordingCarrier : HttpMessageHandler
    {
        public List<(HttpMethod Method, Uri? Uri, string Body)> Requests { get; } = new List<(HttpMethod, Uri?, string)>();

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var body = request.Content == null ? "" : await request.Content.ReadAsStringAsync(cancellationToken);
            Requests.Add((request.Method, request.RequestUri, body));
            return new HttpResponseMessage(HttpStatusCode.Accepted)
            {
                Content = new StringContent("{\"accepted\":true}", Encoding.UTF8, "application/json"),
            };
        }
    }

    public static class CarrierNotificationTests
    {
        private static HttpClient CarrierClient(RecordingCarrier carrier) =>
            new HttpClient(carrier) { BaseAddress = new Uri("https://carrier.acme.example/") };

        private static JsonElement Property(JsonElement document, string name)
        {
            foreach (var property in document.EnumerateObject())
            {
                if (string.Equals(property.Name, name, StringComparison.OrdinalIgnoreCase))
                {
                    return property.Value;
                }
            }

            throw new Exception($"the carrier was not sent {name}");
        }

        private static decimal Number(JsonElement value) =>
            value.ValueKind == JsonValueKind.String
                ? decimal.Parse(value.GetString()!, CultureInfo.InvariantCulture)
                : value.GetDecimal();

        [Test]
        public static async Task A_dispatched_shipment_is_sent_to_the_carrier()
        {
            var carrier = new RecordingCarrier();
            var shipments = new InMemoryShipmentRepository(new Shipment("S-1", "Rotterdam", 12.5m));
            var service = new ShipmentService(shipments, CarrierClient(carrier));

            await service.DispatchAsync("S-1");

            Check.Equal("dispatched", shipments.Get("S-1").Status, "status");
            Check.Equal(1, carrier.Requests.Count, "requests sent to the carrier");
            var (method, uri, body) = carrier.Requests.Single();
            Check.Equal(HttpMethod.Post, method, "method");
            Check.Equal("/v1/dispatches", uri?.AbsolutePath, "path");
            using var json = JsonDocument.Parse(body);
            Check.Equal("S-1", Property(json.RootElement, "shipmentId").GetString(), "shipmentId");
            Check.Equal("Rotterdam", Property(json.RootElement, "destination").GetString(), "destination");
            Check.Equal(12.5m, Number(Property(json.RootElement, "weightKg")), "weightKg");
        }

        [Test]
        public static async Task A_shipment_that_cannot_be_dispatched_is_not_sent()
        {
            var carrier = new RecordingCarrier();
            var gone = new Shipment("S-2", "Lyon", 3m);
            gone.Dispatch(DateTime.UtcNow);
            var service = new ShipmentService(new InMemoryShipmentRepository(gone), CarrierClient(carrier));

            await Check.ThrowsAsync(() => service.DispatchAsync("S-2"), "dispatching twice must fail");
            Check.Equal(0, carrier.Requests.Count, "requests sent to the carrier");
        }
    }
}
