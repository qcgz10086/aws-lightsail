export async function waitForWorkloadData(page, scenario) {
  await page.waitForFunction(config => {
    let element;
    if (config.ready_text) {
      const statistic = Array.from(document.querySelectorAll('.ant-statistic'))
        .find(item => item.textContent.includes(config.ready_text));
      element = statistic?.querySelector('.ant-statistic-content-value');
      if (!element || !/[1-9]/.test(element.textContent)) return false;
    } else {
      element = document.querySelector(config.ready);
    }
    return Boolean(element && element.getClientRects().length);
  }, scenario, { polling: 'raf', timeout: 15000 });
}
