using System.Diagnostics.CodeAnalysis;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ptr727.ProjectTemplate.CodeGen;

internal sealed class ApiNinjas(string apiKey, CancellationToken cancellationToken)
{
    [SuppressMessage(
        "Usage",
        "CA2007:Consider calling ConfigureAwait on the awaited task",
        Justification = "https://github.com/dotnet/roslyn-analyzers/issues/7185"
    )]
    internal async Task<string> GetQuoteOfTheDayAsync()
    {
        // https://api-ninjas.com/api/quotes#v2-quoteoftheday
        using HttpRequestMessage request = new(
            HttpMethod.Get,
            "https://api.api-ninjas.com/v2/quotes?categories=philosophy"
        );
        request.Headers.Add("X-Api-Key", apiKey);

        using HttpResponseMessage response = await HttpClientFactory
            .GetHttpClient()
            .SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
            .ConfigureAwait(false);
        _ = response.EnsureSuccessStatusCode();

        await using Stream responseStream = await response
            .Content.ReadAsStreamAsync(cancellationToken)
            .ConfigureAwait(false);

        QuoteOfTheDayItem[]? items = await JsonSerializer
            .DeserializeAsync(
                responseStream,
                QuoteOfTheDayJsonContext.Default.QuoteOfTheDayItemArray,
                cancellationToken
            )
            .ConfigureAwait(false);

        string? quote = items?.FirstOrDefault()?.Quote;
        return string.IsNullOrWhiteSpace(quote)
            ? throw new InvalidOperationException(
                "Quote of the day response did not include a quote."
            )
            : quote;
    }
}

internal sealed record QuoteOfTheDayItem([property: JsonPropertyName("quote")] string Quote);

[JsonSerializable(typeof(QuoteOfTheDayItem[]))]
internal sealed partial class QuoteOfTheDayJsonContext : JsonSerializerContext;
