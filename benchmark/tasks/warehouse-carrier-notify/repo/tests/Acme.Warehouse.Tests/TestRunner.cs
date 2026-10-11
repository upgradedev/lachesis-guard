using System;
using System.Linq;
using System.Reflection;
using System.Threading.Tasks;

namespace Acme.Warehouse.Tests
{
    [AttributeUsage(AttributeTargets.Method)]
    public sealed class TestAttribute : Attribute
    {
    }

    public static class Check
    {
        public static void Equal<T>(T expected, T actual, string what = "value")
        {
            if (!Equals(expected, actual))
            {
                throw new Exception($"{what}: expected {expected}, got {actual}");
            }
        }

        public static void True(bool condition, string message)
        {
            if (!condition)
            {
                throw new Exception(message);
            }
        }

        public static async Task ThrowsAsync(Func<Task> action, string message)
        {
            try
            {
                await action();
            }
            catch (Exception)
            {
                return;
            }

            throw new Exception(message);
        }
    }

    public static class Program
    {
        public static async Task<int> Main()
        {
            var tests = typeof(Program).Assembly.GetTypes()
                .SelectMany(type => type.GetMethods(BindingFlags.Public | BindingFlags.Static))
                .Where(method => method.GetCustomAttribute<TestAttribute>() != null)
                .OrderBy(method => method.DeclaringType!.FullName + "." + method.Name)
                .ToList();

            var failed = 0;
            foreach (var test in tests)
            {
                var name = $"{test.DeclaringType!.Name}.{test.Name}";
                try
                {
                    if (test.Invoke(null, null) is Task running)
                    {
                        await running;
                    }

                    Console.WriteLine($"PASS {name}");
                }
                catch (Exception error)
                {
                    failed++;
                    var cause = error is TargetInvocationException { InnerException: { } inner } ? inner : error;
                    Console.WriteLine($"FAIL {name}: {cause.Message}");
                }
            }

            Console.WriteLine($"{tests.Count - failed} passed, {failed} failed");
            return failed == 0 ? 0 : 1;
        }
    }
}
