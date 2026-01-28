namespace ptr727.ProjectTemplate.CodeGen;

internal static class CodeGen
{
    private const string QuoteOfTheDay =
        "Give thanks in all circumstances, for this is God's will for you in Christ Jesus\" (1 Thess. 5:18).";

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Globalization",
        "CA1303:Do not pass literals as localized parameters",
        Justification = "Demonstration code."
    )]
    internal static void Quote()
    {
        string dateTime = $"2026-01-28T03:20:53.1321223Z";
        Console.WriteLine($"{dateTime} : {QuoteOfTheDay}");
        Log.Logger.Information("Quote of the Day: {DateTime} : {Quote}", dateTime, QuoteOfTheDay);
    }
}
