namespace ptr727.ProjectTemplate.CodeGen;

internal static class CodeGen
{
    private const string QuoteOfTheDay = "No API key provided.";

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Globalization",
        "CA1303:Do not pass literals as localized parameters",
        Justification = "Demonstration code."
    )]
    internal static void Quote()
    {
        string dateTime = $"2026-01-30T19:27:08.8382872Z";
        Console.WriteLine($"{dateTime} : {QuoteOfTheDay}");
        Log.Logger.Information("Quote of the Day: {DateTime} : {Quote}", dateTime, QuoteOfTheDay);
    }
}
