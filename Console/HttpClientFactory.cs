using System.Net.Http.Headers;
using Microsoft.Extensions.Http.Resilience;
using Polly;

namespace ptr727.ProjectTemplate.Console;

public static class HttpClientFactory
{
    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Design",
        "CA1024:Use properties where appropriate",
        Justification = "Name conflict with known class"
    )]
    public static HttpClient GetHttpClient() => s_httpClient.Value;

    private static readonly Lazy<HttpClient> s_httpClient = new(CreateHttpClient);

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Design",
        "CA1024:Use properties where appropriate",
        Justification = "Name conflict with known class"
    )]
    public static ResilienceHandler GetResilienceHandler() => s_resilienceHandler.Value;

    private static readonly Lazy<ResilienceHandler> s_resilienceHandler = new(
        CreateResilienceHandler
    );

    private static ResilienceHandler CreateResilienceHandler() =>
        new(
            new ResiliencePipelineBuilder<HttpResponseMessage>()
                .AddRetry(
                    new Polly.Retry.RetryStrategyOptions<HttpResponseMessage>
                    {
                        MaxRetryAttempts = 3,
                        BackoffType = DelayBackoffType.Exponential,
                        UseJitter = true,
                        Delay = TimeSpan.FromSeconds(1),
                        MaxDelay = TimeSpan.FromSeconds(30),
                        ShouldHandle = args =>
                            ValueTask.FromResult(
                                args.Outcome.Result != null
                                    && !args.Outcome.Result.IsSuccessStatusCode
                            ),
                    }
                )
                .AddCircuitBreaker(
                    new Polly.CircuitBreaker.CircuitBreakerStrategyOptions<HttpResponseMessage>
                    {
                        FailureRatio = 0.2,
                        MinimumThroughput = 10,
                        SamplingDuration = TimeSpan.FromSeconds(60),
                        BreakDuration = TimeSpan.FromSeconds(30),
                        ShouldHandle = args =>
                            ValueTask.FromResult(
                                args.Outcome.Result != null
                                    && !args.Outcome.Result.IsSuccessStatusCode
                            ),
                    }
                )
                .AddTimeout(TimeSpan.FromSeconds(30))
                .Build()
        )
        {
            InnerHandler = new SocketsHttpHandler
            {
                PooledConnectionLifetime = TimeSpan.FromMinutes(15),
                PooledConnectionIdleTimeout = TimeSpan.FromMinutes(2),
            },
        };

    private static HttpClient CreateHttpClient()
    {
        HttpClient httpClient = new(GetResilienceHandler()) { Timeout = TimeSpan.FromSeconds(120) };
        httpClient.DefaultRequestHeaders.UserAgent.Add(
            new ProductInfoHeaderValue(AssemblyInfo.AppName, AssemblyInfo.InformationalVersion)
        );
        return httpClient;
    }
}
