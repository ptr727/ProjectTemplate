namespace ptr727.ProjectTemplate.CodeGen;

internal static class CodeGen
{
    private const string QuoteOfTheDay =
        "Don't accept a life that has been molded for you by others because eventually you'll succumb to its falseness.";

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Globalization",
        "CA1303:Do not pass literals as localized parameters",
        Justification = "Demonstration code."
    )]
    internal static void Quote()
    {
        string dateTime = $"2026-01-28T04:21:26.4533219Z";
        Console.WriteLine($"{dateTime} : {QuoteOfTheDay}");
        Log.Logger.Information("Quote of the Day: {DateTime} : {Quote}", dateTime, QuoteOfTheDay);
    }
}
