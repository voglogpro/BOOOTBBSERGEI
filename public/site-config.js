// Данные, которые чаще всего меняются. Пустое поле — кнопка на сайте не показывается.
// Адрес города: /krasnodar, /rostov. Ключ города совпадает с адресом.
window.SITE_CONFIG = {
  brand: "Шоколадный фонтан",
  price: "12 000 ₽",
  // Номер счётчика Яндекс Метрики: цели phone, whatsapp, vk, lead_form_open, lead.
  metrikaId: "",
  cities: {
    krasnodar: {
      name: "Краснодар",
      prep: "по Краснодару",
      // Родительный падеж: «за пределы Краснодара».
      gen: "Краснодара",
      in: "в Краснодаре",
      owner: "Ольга",
      ownerDative: "Ольге",
      ownerInstrumental: "Ольгой",
      ownerFull: "Анистратенко Ольга Александровна",
      phone: "+79181123433",
      phoneText: "8 918 112-34-33",
      // Номер WhatsApp в международном формате без +.
      whatsapp: "79181123433",
      vk: "https://vk.ru/id836570910",
      // Ссылка, которая сразу открывает переписку ВКонтакте.
      vkChat: "https://vk.com/write836570910"
    },
    rostov: {
      name: "Ростов-на-Дону",
      prep: "по Ростову-на-Дону",
      gen: "Ростова-на-Дону",
      in: "в Ростове-на-Дону",
      owner: "Анастасия",
      ownerDative: "Анастасии",
      ownerInstrumental: "Анастасией",
      ownerFull: "Остапенко Анастасия Олеговна",
      phone: "+79613237733",
      phoneText: "8 961 323-77-33",
      whatsapp: "79613237733",
      vk: "https://vk.ru/id48331149",
      vkChat: "https://vk.com/write48331149"
    }
  }
};
